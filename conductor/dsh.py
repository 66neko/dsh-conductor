"""用于 ``dsh --profile sdk`` 的小型 JSON-RPC 客户端。"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

type JsonObject = dict[str, Any]
type EventHook = Callable[[JsonObject], None]


class DshError(RuntimeError):
    """DSH 进程或协议执行失败。"""


def resolve_dsh_bin(explicit: str | None = None) -> Path:
    for raw in (explicit, os.environ.get("DSH_BIN")):
        if raw:
            path = Path(raw).expanduser()
            if path.exists():
                return path.resolve()
            found = shutil.which(raw)
            if found:
                return Path(found).resolve()
            raise DshError(f"DSH executable does not exist: {raw}")
    if found := shutil.which("dsh"):
        return Path(found).resolve()
    candidates = (
        Path.home() / "workspace" / "deepseek-harness" / "apps" / "cli" / "lib" / "bin.js",
        Path.home() / "deepseek-harness" / "apps" / "cli" / "lib" / "bin.js",
    )
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise DshError("cannot find DSH; set DSH_BIN or install the dsh executable")


def _supports_import_meta_main(node: Path) -> bool:
    try:
        result = subprocess.run(
            [
                str(node),
                "--input-type=module",
                "-e",
                "process.exit(import.meta.main === true ? 0 : 1)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def resolve_node_bin() -> Path:
    """查找能够执行当前 DSH 入口的 Node 运行时。"""

    if configured := os.environ.get("DSH_NODE"):
        raw = Path(configured).expanduser()
        found = raw if raw.exists() else Path(shutil.which(configured) or raw)
        if not found.is_file():
            raise DshError(f"DSH_NODE does not identify a Node executable: {configured}")
        resolved = found.resolve()
        if not _supports_import_meta_main(resolved):
            raise DshError(
                f"DSH_NODE lacks import.meta.main support required by DSH: {resolved}"
            )
        return resolved

    candidates: list[Path] = []
    if found := shutil.which("node"):
        candidates.append(Path(found))
    nvm_root = Path(os.environ.get("NVM_DIR", Path.home() / ".nvm")).expanduser()
    candidates.extend(sorted((nvm_root / "versions" / "node").glob("*/bin/node"), reverse=True))

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        if _supports_import_meta_main(resolved):
            return resolved
    raise DshError(
        "cannot find a Node runtime with import.meta.main support; "
        "DSH requires Node ^22.19.0 or >=24.0.0 (set DSH_NODE)"
    )


def resolve_dsh_command(explicit: str | None = None) -> list[str]:
    path = resolve_dsh_bin(explicit)
    if path.suffix in {".js", ".mjs"}:
        return [str(resolve_node_bin()), str(path)]
    if path.suffix == ".py":
        return [sys.executable, str(path)]
    return [str(path)]


def probe_dsh(explicit: str | None = None) -> str:
    """在不启动 DSH profile 的情况下探测公开入口。"""

    command = resolve_dsh_command(explicit)
    try:
        result = subprocess.run(
            [*command, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DshError(f"cannot probe DSH command {shlex.join(command)}: {exc}") from exc
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode != 0 or not output:
        detail = output[-1200:] if output else "no output"
        raise DshError(
            f"DSH probe failed (exit {result.returncode}) for {shlex.join(command)}: {detail}"
        )
    return f"{output.splitlines()[-1]} via {shlex.join(command)}"


@dataclass(frozen=True, slots=True)
class DshConfig:
    workspace: Path
    dsh_bin: str | None = None
    profile: str = "sdk"
    provider: str = "deepseek-official"
    model: str = "deepseek-flash"
    dsh_home: Path | None = None
    skill_dir: Path | None = None
    init_timeout_seconds: float = 30.0
    shutdown_timeout_seconds: float = 5.0
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunResult:
    session_id: str
    status: str
    final_text: str
    turn_end_reason: JsonObject | None
    events: tuple[JsonObject, ...]
    elapsed_seconds: float
    stderr_tail: str

    @property
    def ok(self) -> bool:
        return self.status == "completed"


@dataclass(slots=True)
class _PendingRequest:
    ready: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: str | None = None


def _last_assistant_text(events: list[JsonObject]) -> str:
    for event in reversed(events):
        if event.get("type") != "assistant/message":
            continue
        message = (event.get("data") or {}).get("message") or {}
        blocks = message.get("content") or []
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text.strip():
            return text
    return ""


class DshClient:
    """独占一个 DSH 运行时进程，并串行执行 prompt。"""

    def __init__(self, config: DshConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._pending: dict[int, _PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stderr: deque[str] = deque(maxlen=200)
        self._protocol_errors: deque[str] = deque(maxlen=20)
        self._events: list[JsonObject] = []
        self._turn_end_reason: JsonObject | None = None
        self._session_status: str | None = None
        self._turn_finished = threading.Event()
        self._process_exited = threading.Event()
        self._stderr_finished = threading.Event()
        self._event_hook: EventHook | None = None
        self._initialized = False
        self._closed = False

    def _command(self) -> list[str]:
        return [*resolve_dsh_command(self.config.dsh_bin), "--profile", self.config.profile]

    def start(self) -> "DshClient":
        if self._process is not None:
            return self
        environment = os.environ.copy()
        environment.update(self.config.extra_env)
        package_root = str(Path(__file__).resolve().parent.parent)
        python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            package_root
            if not python_path
            else os.pathsep.join((package_root, python_path))
        )
        # 这是 DSH 执行 skill 和验收命令的必要条件，调用方不能通过 extra_env 覆盖。
        environment["DSH_PERMISSION_MODE"] = "danger-full-access"
        if self.config.dsh_home is not None:
            environment["DSH_HOME"] = str(self.config.dsh_home.expanduser().resolve())
        if self.config.skill_dir is not None:
            # 让 DSH 将 SDK 注入的项目级目录作为随包 skill 根目录扫描。
            environment["DSH_BUNDLED_SKILL_DIR"] = str(self.config.skill_dir.expanduser().resolve())
        try:
            self._process = subprocess.Popen(
                self._command(),
                cwd=self.config.workspace,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise DshError(f"cannot start DSH: {exc}") from exc
        threading.Thread(target=self._read_stdout, name="dsh-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="dsh-stderr", daemon=True).start()
        return self

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for raw_line in self._process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as exc:
                self._protocol_errors.append(f"invalid JSON-RPC frame: {exc}: {line[:200]}")
                continue
            if not isinstance(frame, dict):
                self._protocol_errors.append(f"non-object JSON-RPC frame: {line[:200]}")
                continue
            self._dispatch(frame)
        # stdout 关闭代表进程已退出；等待 stderr 线程收尾，确保错误信息不会丢失。
        return_code = self._process.wait()
        self._stderr_finished.wait(1.0)
        detail = self.stderr_tail().strip()
        exit_error = f"DSH process exited with code {return_code} before responding"
        if detail:
            exit_error += f": {detail[-1200:]}"
        self._process_exited.set()
        with self._pending_lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
        for request in pending:
            request.error = exit_error
            request.ready.set()
        self._turn_finished.set()

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            for line in self._process.stderr:
                self._stderr.append(line)
        finally:
            self._stderr_finished.set()

    def _dispatch(self, frame: JsonObject) -> None:
        request_id = frame.get("id")
        if isinstance(request_id, int) and "method" not in frame:
            with self._pending_lock:
                request = self._pending.pop(request_id, None)
            if request is None:
                return
            error = frame.get("error")
            if isinstance(error, dict):
                request.error = f"{error.get('code')}: {error.get('message')}"
            else:
                request.result = frame.get("result")
            request.ready.set()
            return

        method = frame.get("method")
        params = frame.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "session.event":
            event = params.get("event")
            if not isinstance(event, dict):
                return
            self._events.append(event)
            if hook := self._event_hook:
                try:
                    hook(event)
                except Exception:
                    pass
            if event.get("type") == "turn/end":
                data = event.get("data")
                data = data if isinstance(data, dict) else {}
                reason = data.get("reason")
                self._turn_end_reason = reason if isinstance(reason, dict) else {}
                self._maybe_finish_turn()
        elif method == "session.status":
            status = params.get("status")
            self._session_status = status if isinstance(status, str) else None
            self._maybe_finish_turn()

    def _maybe_finish_turn(self) -> None:
        # 两条通知到达顺序不固定，只有 turn/end 与 idle 同时成立才算协议完成。
        if self._turn_end_reason is not None and self._session_status == "idle":
            self._turn_finished.set()

    def _rpc(self, method: str, params: JsonObject | None, *, timeout_seconds: float) -> object:
        if self._process is None or self._process.stdin is None:
            raise DshError("DSH process is not running")
        # 先登记 pending 再写 stdin，避免极快响应先于等待对象注册。
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            pending = _PendingRequest()
            self._pending[request_id] = pending
        frame: JsonObject = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        try:
            with self._write_lock:
                self._process.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise DshError(f"cannot write JSON-RPC request {method}: {exc}") from exc
        if not pending.ready.wait(timeout_seconds):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise DshError(f"JSON-RPC request {method} timed out after {timeout_seconds:g}s")
        if pending.error is not None:
            raise DshError(f"JSON-RPC request {method} failed: {pending.error}")
        return pending.result

    def initialize(self) -> None:
        if self._initialized:
            return
        self._rpc(
            "initialize",
            {
                "cwd": str(self.config.workspace.resolve()),
                "provider": self.config.provider,
                "model": self.config.model,
            },
            timeout_seconds=self.config.init_timeout_seconds,
        )
        self._initialized = True

    def run(
        self,
        prompt: str,
        *,
        session_id: str,
        timeout_seconds: float,
        on_event: EventHook | None = None,
    ) -> RunResult:
        if self._process is None:
            self.start()
        self.initialize()
        self._events = []
        self._turn_end_reason = None
        self._session_status = None
        self._turn_finished.clear()
        self._event_hook = on_event
        started = time.monotonic()
        self._rpc(
            "session/prompt",
            {
                "sessionId": session_id,
                "contentBlocks": [{"type": "text", "text": prompt}],
            },
            timeout_seconds=min(60.0, timeout_seconds),
        )
        finished = self._turn_finished.wait(max(0.0, timeout_seconds - (time.monotonic() - started)))
        elapsed = time.monotonic() - started
        process_exited = self._process_exited.is_set()
        reason_kind = (self._turn_end_reason or {}).get("kind")
        if not finished:
            status = "timeout"
        elif process_exited and self._turn_end_reason is None:
            status = "error"
        elif reason_kind == "completed":
            status = "completed"
        elif reason_kind == "interrupted":
            status = "interrupted"
        else:
            status = "error"
        diagnostics = self.stderr_tail()
        if self._protocol_errors:
            diagnostics += "".join(f"\n{line}" for line in self._protocol_errors)
        return RunResult(
            session_id=session_id,
            status=status,
            final_text=_last_assistant_text(self._events),
            turn_end_reason=self._turn_end_reason,
            events=tuple(self._events),
            elapsed_seconds=elapsed,
            stderr_tail=diagnostics.strip(),
        )

    def stderr_tail(self, lines: int = 30) -> str:
        return "".join(tuple(self._stderr)[-lines:])

    def close(self) -> None:
        if self._closed or self._process is None:
            return
        self._closed = True
        if self._process.poll() is None:
            try:
                self._rpc("shutdown", None, timeout_seconds=self.config.shutdown_timeout_seconds)
            except DshError:
                pass
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except OSError:
                pass
        try:
            self._process.wait(timeout=self.config.shutdown_timeout_seconds)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        for stream in (self._process.stdout, self._process.stderr):
            if stream is not None:
                stream.close()

    def __enter__(self) -> "DshClient":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()
