"""本轮资源登记、可核验的清理报告和崩溃后的幂等回收。"""

from __future__ import annotations

import os
import signal
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import OperationError
from .lifecycle import Budget, RunContext, acquire_lock, positive_seconds
from .models import read_json_object
from .processes import (
    boot_id, identities, process_identity, register_process, same_process,
    snapshot_group, stop_process_group, validate_process_record,
)
from .state import RunState, atomic_write_json
from .tmux import TmuxSession, _run


@dataclass(frozen=True, slots=True)
class CleanupReport:
    status: str = "completed"
    elapsed_seconds: float = 0.0
    timed_out: bool = False
    remaining_resources: tuple[dict[str, Any], ...] = ()
    errors: tuple[str, ...] = ()
    retained_resources: tuple[dict[str, Any], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "elapsed_seconds": round(self.elapsed_seconds, 3),
                "timed_out": self.timed_out, "remaining_resources": list(self.remaining_resources),
                "errors": list(self.errors), "retained_resources": list(self.retained_resources)}


class RunResources:
    def __init__(self, state: RunState, context: RunContext) -> None:
        self.path = state.root / "runtime.json"
        self.lock = (state.root / "run.lock").open("a")
        self.socket_directory: Path | None = None
        try:
            acquire_lock(self.lock, context.budget(), immediate=True)
            # 短路径且目录只允许当前用户访问，避免 workspace 路径超过 Unix socket 限制。
            self.socket_directory = Path(tempfile.mkdtemp(prefix="dshc-", dir="/tmp"))
            identity = self.socket_directory.stat()
            self.data = {
                "schema_version": 1, "run_id": state.run_id, "workspace": str(state.workspace),
                "state_directory": str(state.root), "owner": process_identity(os.getpid()), "active": True,
                "tmux_socket": str(self.socket_directory / "tmux.sock"),
                "socket_directory": str(self.socket_directory),
                "socket_identity": {"device": identity.st_dev, "inode": identity.st_ino, "uid": identity.st_uid},
                "sessions": {agent.session: agent.kind.value for agent in state.agents},
                "execution_deadline": context.execution_deadline, "boot_id": boot_id(),
            }
            atomic_write_json(self.path, self.data)
        except BaseException:
            self.release()
            if self.socket_directory is not None:
                self.socket_directory.rmdir()
            raise

    @property
    def socket(self) -> Path:
        return Path(self.data["tmux_socket"])

    def finish(self, report: CleanupReport) -> None:
        self.data.update(active=False, cleanup=report.to_json())
        atomic_write_json(self.path, self.data)

    def release(self) -> None:
        self.lock.close()


def controller_settings(request: dict[str, Any], root: Path) -> tuple[Path | None, Budget | None, Path | None]:
    path = root / "runtime.json"
    if not path.exists():
        return None, None, None  # 独立控制器/旧运行记录仍可读取，不承诺它们的恢复清理。
    runtime = read_json_object(path)
    if (runtime.get("run_id") != request["run_id"] or runtime.get("boot_id") != boot_id()
            or not runtime.get("active")):
        raise OperationError("run context is no longer active on this boot", code="cancelled", phase="worker_run")
    budget = Budget(runtime["execution_deadline"], "worker_run", stop_file=root / "sdk-stop.json", runtime_file=path)
    budget.check()
    return Path(runtime["tmux_socket"]), budget, path


def _socket_owned(runtime: dict[str, Any]) -> bool:
    directory = Path(runtime["socket_directory"])
    socket = Path(runtime["tmux_socket"])
    if socket.parent != directory or socket.name != "tmux.sock" or directory.is_symlink():
        raise ValueError("invalid private socket directory")
    if not directory.exists():
        return False
    current = directory.stat()
    expected = runtime["socket_identity"]
    if ({"device": current.st_dev, "inode": current.st_ino, "uid": current.st_uid} != expected
            or current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) != 0o700):
        raise ValueError("private socket directory identity does not match this run")
    if socket.is_symlink():
        raise ValueError("private tmux socket is a symbolic link")
    return True


def cleanup_resources(runtime: dict[str, Any], budget: Budget, *, retain_session: str | None = None) -> CleanupReport:
    started = time.monotonic()
    errors: list[str] = []
    remaining: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    retained_identities: list[dict[str, Any]] = []
    timed_out = False
    root = Path(runtime["state_directory"])
    socket = Path(runtime["tmux_socket"])
    records: list[dict[str, Any]] = []
    try:
        socket_owned = _socket_owned(runtime)
    except (OSError, ValueError, KeyError) as exc:
        socket_owned = False
        errors.append(str(exc))
        remaining.append({"kind": "tmux_socket", "path": str(socket), "reason": "ownership_unverified"})

    # 先观察进程，再关闭 tmux；worker leader 退出后仍能确认原来的组成员。
    for path in (root / "resources").glob("*.json"):
        try:
            record = read_json_object(path)
            if record.get("run_id") != runtime["run_id"] or not isinstance(record.get("identity"), dict):
                raise ValueError("resource identity does not match this run")
            validate_process_record(record)
            record["record_file"] = str(path)
            snapshot_group(record)
            records.append(record)
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"{path.name}: {exc}")
            remaining.append({"kind": "resource", "path": str(path), "reason": "ownership_unverified"})

    # cleanup_run 可能在 SDK 被 SIGKILL 后运行；先终止遗留管理者和工具，
    # 再接触 tmux，避免清理期间又创建 worker。SDK 正常 finally 也可重复执行。
    for record in records:
        if record.get("kind") in {"dsh", "command"}:
            try:
                stop_process_group(record, signal.SIGTERM)
                stop_process_group(record, signal.SIGKILL)
            except OSError as exc:
                errors.append(f"manager cleanup: {exc}")

    if socket_owned and socket.exists():
        try:
            result = _run(["list-sessions", "-F", "#{session_name}"], socket_path=socket, budget=budget)
            names = result.stdout.splitlines()
            retained_worker = None
            for name in names:
                session = TmuxSession(name, socket_path=socket, budget=budget)
                status = session.status()
                if (name not in runtime["sessions"] or status.run_id != runtime["run_id"]
                        or status.agent != runtime["sessions"][name] or status.workspace != runtime["workspace"]):
                    raise ValueError(f"tmux session {name} ownership does not match this run")
                if name == retain_session and not status.pane_dead and status.pane_pid is not None:
                    retained_worker = process_identity(status.pane_pid)
            if names:
                server_pid = int(_run(["display-message", "-p", "-t", f"={names[0]}:", "#{pid}"],
                                      socket_path=socket, budget=budget).stdout.strip())
                record = register_process(server_pid, "tmux", root / "runtime.json")
                records.append(record)
            if retain_session in names:
                for name in names:
                    session = TmuxSession(name, socket_path=socket, budget=budget)
                    if name == retain_session:
                        # 保留 worker 不意味着继续运行活动采集辅助进程。
                        session._run(["pipe-pane", "-t", session._pane_target()])
                        retained.append({"kind": "worker_session", "session": name, "tmux_socket": str(socket)})
                        retained_identities.extend(item for item in (record["identity"], retained_worker) if item)
                    else:
                        session.close()
            else:
                # 只会在核验过的本轮私有 socket 上执行。
                _run(["kill-server"], socket_path=socket, budget=budget, check=False)
        except (OSError, ValueError, OperationError) as exc:
            timed_out |= isinstance(exc, OperationError) and exc.timed_out
            errors.append(f"tmux cleanup: {exc}")
            # 服务已退出时 socket 文件仍可能短暂存在，最终以资源身份核验为准。
            if isinstance(exc, ValueError):
                remaining.append({"kind": "tmux_socket", "path": str(socket), "reason": "ownership_unverified"})

    # 保留所选 pane 和当前 server，其他候选或已替换 worker 的残留进程仍须回收。
    managed = [record for record in records if not (
        record.get("kind") in {"tmux", "worker"}
        and any(same_process(record.get("identity") or {}, item) for item in retained_identities)
    )]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for record in managed:
            try:
                stop_process_group(record, sig)
            except (OSError, ValueError) as exc:
                errors.append(f"process cleanup: {exc}")
        wait_until = (time.monotonic() + max(0, budget.deadline - time.monotonic()) * 0.25
                      if sig == signal.SIGTERM else budget.deadline)
        while time.monotonic() < wait_until:
            if not any(snapshot_group(record) for record in managed):
                break
            time.sleep(min(0.02, max(0, wait_until - time.monotonic())))
    for record in managed:
        alive = snapshot_group(record)
        if alive:
            remaining.append({"kind": record["kind"], "processes": alive})
        # 无可信 anchor 但旧 session ID 仍有进程时，不猜测其归属。
        expected = record.get("identity")
        if expected and expected.get("boot_id") == boot_id() and not alive and not record.get("individual"):
            candidates = [p for p in identities() if p["sid"] == expected["sid"] and p["state"] != "Z"]
            leader = process_identity(expected["pid"])
            if candidates and leader is None:
                remaining.append({"kind": record["kind"], "reason": "group_identity_unverified", "sid": expected["sid"]})
            elif leader is not None and not same_process(expected, leader):
                errors.append(f"PID {expected['pid']} has been reused; unrelated process was left untouched")

    # tmux RPC 失败且没有 server 登记时，不能把无法确认的服务器当成已退出。
    if socket_owned and socket.exists() and not retained:
        try:
            result = _run(["list-sessions"], socket_path=socket, budget=budget, check=False)
            if result.returncode == 0:
                remaining.append({"kind": "tmux_socket", "path": str(socket)})
            elif not any(message in result.stderr.lower() for message in ("no server running", "no such file", "connection refused")):
                remaining.append({"kind": "tmux_socket", "path": str(socket), "reason": result.stderr.strip()})
        except (OSError, OperationError) as exc:
            timed_out |= isinstance(exc, OperationError) and exc.timed_out
            errors.append(f"cannot verify tmux shutdown: {exc}")
            remaining.append({"kind": "tmux_socket", "path": str(socket), "reason": "shutdown_unverified"})
    if not remaining and not retained and socket_owned:
        try:
            socket.unlink(missing_ok=True)
            Path(runtime["socket_directory"]).rmdir()
        except OSError as exc:
            errors.append(f"socket directory cleanup: {exc}")
            remaining.append({"kind": "socket_directory", "path": runtime["socket_directory"]})
    for reaper in budget.reapers:
        reaper.join(timeout=max(0, min(0.05, budget.deadline - time.monotonic())))
        if reaper.is_alive():
            remaining.append({"kind": "thread", "name": reaper.name})
    timed_out |= bool(remaining and time.monotonic() >= budget.deadline)
    return CleanupReport("incomplete" if remaining else "retained" if retained else "completed",
                         time.monotonic() - started, timed_out, tuple(remaining), tuple(errors), tuple(retained))


def cleanup_run(state_directory: str | Path, *, timeout_seconds: float = 5.0) -> CleanupReport:
    """清理已停止的 run；活动 run、旧记录或归属不明时返回 incomplete，不猜测 PID。"""
    positive_seconds(timeout_seconds, "timeout_seconds")
    started = time.monotonic()
    root = Path(state_directory).expanduser().resolve()
    budget = Budget(started + timeout_seconds, "cleanup")
    try:
        runtime = read_json_object(root / "runtime.json")
        if (runtime.get("schema_version") != 1 or runtime.get("state_directory") != str(root)
                or runtime.get("run_id") != read_json_object(root / "request.json")["run_id"]):
            raise ValueError("runtime metadata does not identify this run")
        with (root / "run.lock").open("a") as handle:
            acquire_lock(handle, budget, immediate=True)
            atomic_write_json(root / "sdk-stop.json", {"run_id": runtime["run_id"], "reason": "cleanup_run"})
            report = cleanup_resources(runtime, budget)
            runtime.update(active=False, cleanup=report.to_json())
            atomic_write_json(root / "runtime.json", runtime)
            return report
    except (OSError, ValueError, KeyError, TypeError, OperationError) as exc:
        return CleanupReport("incomplete", time.monotonic() - started,
                             isinstance(exc, OperationError) and exc.timed_out,
                             ({"kind": "run", "path": str(root), "reason": "cleanup_unavailable"},), (str(exc),))
