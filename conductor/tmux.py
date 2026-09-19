"""供不同 agent 控制器共用的精确 tmux 传输层。"""

from __future__ import annotations

import os
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .errors import OperationError
from .lifecycle import Budget, run_command
from .processes import register_process


class TmuxError(OperationError):
    """tmux 操作失败，或操作目标不是预期会话。"""


_SESSION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HISTORY_LIMIT = 50000
DEFAULT_CAPTURE_HISTORY_LINES = 5000


def _run(
    arguments: Sequence[str], *, input_text: str | None = None, timeout_seconds: float = 20.0,
    check: bool = True, socket_path: Path | None = None, budget: Budget | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["tmux"]
    if socket_path is not None:
        command.extend(["-S", str(socket_path), "-f", "/dev/null"])
    try:
        result = run_command([*command, *arguments], input_text=input_text,
                             timeout_seconds=timeout_seconds, budget=budget)
    except OperationError as exc:
        raise TmuxError(str(exc), code=exc.code, phase=exc.phase, details=exc.details) from exc
    except OSError as exc:
        raise TmuxError(f"tmux invocation failed: {exc}",
                        code="dependency_missing" if isinstance(exc, FileNotFoundError) else "dsh_execution_failed",
                        phase=budget.phase if budget else "worker_run") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise TmuxError(f"tmux {' '.join(arguments)} failed: {detail}", code="dsh_execution_failed",
                        phase=budget.phase if budget else "worker_run")
    return result


def validate_session_name(name: str) -> str:
    if not _SESSION_NAME.fullmatch(name):
        raise TmuxError("session name must match [A-Za-z0-9_-]{1,64}")
    return name


@dataclass(frozen=True, slots=True)
class SessionStatus:
    name: str
    agent: str
    workspace: str
    pane_dead: bool
    pane_pid: int | None
    socket_path: Path | None = None
    run_id: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "session": self.name,
            "agent": self.agent,
            "workspace": self.workspace,
            "pane_dead": self.pane_dead,
            "pane_pid": self.pane_pid,
            "attach_command": shlex.join(["tmux", *(["-S", str(self.socket_path)] if self.socket_path else []),
                                           "attach", "-t", self.name]),
            **({"tmux_socket": str(self.socket_path)} if self.socket_path else {}),
            **({"run_id": self.run_id} if self.run_id else {}),
        }


class TmuxSession:
    """带 conductor 元数据标记的分离式 tmux 会话。"""

    def __init__(self, name: str, *, socket_path: Path | None = None, budget: Budget | None = None,
                 runtime_file: Path | None = None) -> None:
        self.name = validate_session_name(name)
        self.socket_path = socket_path
        self.budget = budget
        self.runtime_file = runtime_file
        # tmux 默认把 -t 当成前缀匹配；缺失的候选会话不能匹配到另一会话。
        self.target = f"={self.name}:"

    def _run(self, arguments: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _run(arguments, socket_path=self.socket_path, budget=self.budget, **kwargs)

    @classmethod
    def create(
        cls,
        *,
        name: str,
        workspace: Path,
        agent: str,
        command: Sequence[str],
        width: int = 200,
        height: int = 50,
        activity_file: Path | None = None,
        socket_path: Path | None = None,
        budget: Budget | None = None,
        runtime_file: Path | None = None,
    ) -> "TmuxSession":
        session = cls(name, socket_path=socket_path, budget=budget, runtime_file=runtime_file)
        if session.exists():
            raise TmuxError(f"tmux session already exists: {name}")
        root = workspace.expanduser().resolve()
        if not root.is_dir():
            raise TmuxError(f"workspace is not a directory: {root}")
        if not command:
            raise TmuxError("agent command is empty")
        shell_command = "exec " + shlex.join(command)
        arguments = [
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{window_id}",
            "-s",
            name,
            "-x",
            str(width),
            "-y",
            str(height),
            "-c",
            str(root),
        ]
        if path_value := os.environ.get("PATH"):
            arguments.extend(["-e", f"PATH={path_value}"])
        # 已存在的 tmux server 不会自动继承调用方的 PYTHONPATH。worker 的 complete
        # 入口也必须导入本次 SDK 版本，而非系统安装的旧包或根本找不到 conductor。
        package_root = str(Path(__file__).resolve().parent.parent)
        python_paths = [package_root, *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))]
        arguments.extend(["-e", f"PYTHONPATH={os.pathsep.join(dict.fromkeys(python_paths))}"])
        # history-limit 只影响新窗口。先建占位窗口，设置本会话上限后再建 worker 窗口；
        # 不修改全局选项，也不让 worker 在历史缓冲区配置好之前输出。
        arguments.append("exec sleep 86400")
        bootstrap_window = session._run(arguments).stdout.strip()
        try:
            if runtime_file is not None:
                server_pid = int(session._run(["display-message", "-p", "-t", session.target, "#{pid}"]).stdout.strip())
                register_process(server_pid, "tmux", runtime_file)
            session._run(["set-option", "-t", session.target, "history-limit", str(HISTORY_LIMIT)])
            if runtime_file is not None:
                runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
                session._run(["set-option", "-t", session.target, "@dsh_conductor_run_id", runtime["run_id"]])
            # agent 与工作区写入会话元数据，后续控制器据此拒绝误接管。
            session._run(["set-option", "-t", session.target, "@dsh_conductor_agent", agent])
            session._run(["set-option", "-t", session.target, "@dsh_conductor_workspace", str(root)])
            pane = session._run([
                "new-window", "-P", "-F", "#{pane_id}", "-t", session.target,
                "-n", agent, "-c", str(root), "exec sleep 86400",
            ]).stdout.strip()
            session._run(["set-option", "-t", session.target, "@dsh_conductor_pane", pane])
            session._run(["set-option", "-p", "-t", pane, "remain-on-exit", "on"])
            session._run(["kill-window", "-t", bootstrap_window])
            if activity_file is not None:
                activity_file.parent.mkdir(parents=True, exist_ok=True)
                activity_file.write_text('{"bytes": 0, "last_output_at": 0}', encoding="utf-8")
                collector = shlex.join([sys.executable, str(Path(__file__).with_name("activity.py")), str(activity_file),
                                        *([str(runtime_file)] if runtime_file else [])])
                session._run(["pipe-pane", "-O", "-t", pane, collector])
                if runtime_file is not None:
                    registration = (budget or Budget(float("inf"))).limit(5, phase="worker_start")
                    while not json.loads(activity_file.read_text(encoding="utf-8")).get("pid"):
                        registration.sleep(0.02)
            session._run(["respawn-pane", "-k", "-t", pane, shell_command])
            if runtime_file is not None:
                pane_pid = int(session._run(["display-message", "-p", "-t", pane, "#{pane_pid}"]).stdout.strip())
                register_process(pane_pid, "worker", runtime_file)
        except BaseException:
            # 使用独立收尾预算；SDK 的资源登记还会兜底回收整个 socket。
            try:
                cls(name, socket_path=socket_path, budget=Budget(time.monotonic() + 0.2, "cleanup")).close()
            except (TmuxError, OSError):
                pass
            raise
        return session

    @classmethod
    def attach(cls, *, name: str, expected_agent: str, socket_path: Path | None = None,
               budget: Budget | None = None, runtime_file: Path | None = None) -> "TmuxSession":
        session = cls(name, socket_path=socket_path, budget=budget, runtime_file=runtime_file)
        if not session.exists():
            raise TmuxError(f"tmux session does not exist: {name}")
        actual = session._option("@dsh_conductor_agent")
        if actual != expected_agent:
            raise TmuxError(
                f"session {name!r} belongs to {actual or 'an unknown process'}, not {expected_agent}"
            )
        return session

    def exists(self) -> bool:
        result = self._run(["has-session", "-t", self.target], check=False)
        if result.returncode == 0:
            return True
        detail = result.stderr.lower()
        if any(message in detail for message in ("can't find session", "no server running", "no sessions",
                                                   "no such file or directory", "connection refused")):
            return False
        raise TmuxError(f"cannot check worker session: {result.stderr.strip()}", code="dsh_execution_failed",
                        phase=self.budget.phase if self.budget else "worker_run")

    def _option(self, name: str) -> str:
        return self._run(["show-option", "-qv", "-t", self.target, name]).stdout.strip()

    def _pane_target(self) -> str:
        pane = self._option("@dsh_conductor_pane")
        if not pane:
            # 兼容旧版本创建的单 pane 会话。
            return self.target
        if not re.fullmatch(r"%\d+", pane):
            raise TmuxError("invalid worker pane metadata")
        owner = self._run(["display-message", "-p", "-t", pane, "#{session_name}"]).stdout.strip()
        if owner != self.name:
            raise TmuxError("worker pane no longer belongs to this session")
        return pane

    def capture(self, *, history_lines: int = 0, join_wrapped: bool = True) -> str:
        arguments = ["capture-pane", "-p", "-t", self._pane_target()]
        if join_wrapped:
            arguments.append("-J")
        if history_lines > 0:
            arguments.extend(["-S", f"-{history_lines}"])
        return self._run(arguments, timeout_seconds=30.0).stdout

    def activity_status(self) -> str:
        """保留光标位置、显示/闪烁模式和管道状态，不能使用展示日志的归一化结果。"""
        return self._run([
            "display-message", "-p", "-t", self._pane_target(),
            "#{cursor_x}:#{cursor_y}:#{cursor_flag}:#{cursor_blink}:#{pane_pipe}",
        ]).stdout.strip()

    def send_text(
        self,
        text: str,
        *,
        submit: bool = True,
        submit_delay_seconds: float = 1.0,
    ) -> None:
        # 使用 tmux buffer 传递字面文本，避免任务内容被 shell 或 send-keys 解释。
        buffer_name = f"dsh-{self.name}-{os.getpid()}-{time.monotonic_ns()}"
        pane = self._pane_target()
        self._run(["load-buffer", "-b", buffer_name, "-"], input_text=text)
        try:
            # -p 在应用启用 bracketed paste 时发送边界，避免正文换行被当作按键，
            # 也避免 Codex 的 paste-burst 检测把随后 Enter 吸收为正文换行。
            self._run(["paste-buffer", "-p", "-d", "-b", buffer_name, "-t", pane])
        finally:
            self._run(["delete-buffer", "-b", buffer_name], check=False)
        if submit:
            # 全屏 TUI 会异步处理 bracketed paste；立即发送 Enter 可能早于粘贴完成，
            # 导致完整 prompt 仍停留在编辑器中而没有提交。
            if self.budget is None:
                time.sleep(submit_delay_seconds)
            else:
                self.budget.sleep(submit_delay_seconds)
            self.send_keys("Enter")

    def send_keys(self, *keys: str) -> None:
        if keys:
            self._run(["send-keys", "-t", self._pane_target(), *keys])

    def status(self) -> SessionStatus:
        fields = self._run(
            ["display-message", "-p", "-t", self._pane_target(), "#{pane_dead}\t#{pane_pid}"],
        ).stdout.strip().split("\t")
        pane_pid = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None
        return SessionStatus(
            name=self.name,
            agent=self._option("@dsh_conductor_agent"),
            workspace=self._option("@dsh_conductor_workspace"),
            pane_dead=bool(fields and fields[0] == "1"),
            pane_pid=pane_pid,
            socket_path=self.socket_path,
            run_id=self._option("@dsh_conductor_run_id") or None,
        )

    def close(self) -> None:
        result = self._run(["kill-session", "-t", self.target], check=False)
        if result.returncode and self.exists():
            raise TmuxError(f"cannot stop worker session {self.name}: {result.stderr.strip()}")

    @staticmethod
    def list_tagged(*, socket_path: Path | None = None) -> list[SessionStatus]:
        result = _run(["list-sessions", "-F", "#{session_name}"], check=False, socket_path=socket_path)
        if result.returncode != 0:
            return []
        sessions: list[SessionStatus] = []
        for name in result.stdout.splitlines():
            if not name.strip():
                continue
            session = TmuxSession(name.strip(), socket_path=socket_path)
            if session._option("@dsh_conductor_agent"):
                sessions.append(session.status())
        return sessions
