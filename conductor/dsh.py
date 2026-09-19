"""用于 ``dsh --profile sdk`` 的小型 JSON-RPC 客户端。"""

from __future__ import annotations

import json
import os
import queue
import shlex
import selectors
import signal
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import OperationError
from .lifecycle import Budget, POLL_SECONDS, run_command
from .processes import boot_id, register_process, snapshot_group, stop_process_group

type JsonObject = dict[str, Any]
type EventHook = Callable[[JsonObject], None]


class DshError(OperationError):
    """带来源分类的 DSH 进程或协议错误。"""

    def __init__(self, message: str, *, code: str = "dependency_missing",
                 phase: str = "dsh_start", details: JsonObject | None = None) -> None:
        super().__init__(message, code=code, phase=phase, details=details)


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


def _supports_import_meta_main(node: Path, budget: Budget | None = None) -> bool:
    try:
        result = run_command(
            [str(node), "--input-type=module", "-e", "process.exit(import.meta.main === true ? 0 : 1)"],
            budget=budget, timeout_seconds=5,
        )
    except OSError:
        return False
    return result.returncode == 0


def resolve_node_bin(budget: Budget | None = None) -> Path:
    """查找能够执行当前 DSH 入口的 Node 运行时。"""

    if configured := os.environ.get("DSH_NODE"):
        raw = Path(configured).expanduser()
        found = raw if raw.exists() else Path(shutil.which(configured) or raw)
        if not found.is_file():
            raise DshError(f"DSH_NODE does not identify a Node executable: {configured}")
        resolved = found.resolve()
        if not _supports_import_meta_main(resolved, budget):
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
        if _supports_import_meta_main(resolved, budget):
            return resolved
    raise DshError(
        "cannot find a Node runtime with import.meta.main support; "
        "DSH requires Node ^22.19.0 or >=24.0.0 (set DSH_NODE)"
    )


def resolve_dsh_command(explicit: str | None = None, budget: Budget | None = None) -> list[str]:
    path = resolve_dsh_bin(explicit)
    if path.suffix in {".js", ".mjs"}:
        return [str(resolve_node_bin(budget)), str(path)]
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
    budget: Budget | None = None


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
    method: str = ""
    ready: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: str | None = None


class _HookDispatcher:
    """低层 DshClient 的调用方 hook 同样不能占用协议泵。"""

    def __init__(self, callback: EventHook) -> None:
        self.callback = callback
        self.events: queue.Queue[JsonObject | None] = queue.Queue(maxsize=1024)
        self.stopped = threading.Event()
        self.inflight = False
        self.thread = threading.Thread(target=self._loop, name="dsh-events", daemon=True)
        self.thread.start()

    def emit(self, event: JsonObject | None) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            try:
                self.events.put_nowait(event)
            except queue.Full:
                pass

    def _loop(self) -> None:
        while not self.stopped.is_set():
            try:
                event = self.events.get(timeout=0.1)
            except queue.Empty:
                continue
            if event is None or self.stopped.is_set():
                return
            self.inflight = True
            try:
                self.callback(event)
            except Exception:
                pass
            finally:
                self.inflight = False

    def close(self, deadline: float) -> None:
        self.emit(None)
        self.thread.join(timeout=max(0, min(0.1, deadline - time.monotonic())))
        self.stopped.set()
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break


def _last_assistant_text(events: list[JsonObject]) -> str:
    for event in reversed(events):
        if event.get("type") != "assistant/message":
            continue
        data = event.get("data")
        message = data.get("message") if isinstance(data, dict) else None
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            continue
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        )
        if text.strip():
            return text
    return ""


class DshClient:
    """独占一个 DSH 进程。管道无缓冲、非阻塞，不依赖读线程退出。"""

    def __init__(self, config: DshConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._selector = selectors.DefaultSelector()
        self._next_id = 1
        self._pending: dict[int, _PendingRequest] = {}
        self._stderr: deque[str] = deque(maxlen=200)
        self._buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self._events: list[JsonObject] = []
        self._turn_end_reason: JsonObject | None = None
        self._session_status: str | None = None
        self._event_hook: _HookDispatcher | None = None
        self._initialized = False
        self._closed = False
        self._fatal: DshError | None = None
        self._resource: JsonObject = {}
        self.close_errors: list[str] = []
        self._budget = config.budget or Budget(float("inf"), "dsh_start")

    def _command(self) -> list[str]:
        return [*resolve_dsh_command(self.config.dsh_bin, self._budget), "--profile", self.config.profile]

    def start(self) -> DshClient:
        if self._process is not None:
            return self
        self._budget.check()
        if not boot_id():
            raise DshError("verified process cleanup requires Linux /proc", code="dependency_missing")
        environment = os.environ.copy()
        environment.update(self.config.extra_env)
        package_root = str(Path(__file__).resolve().parent.parent)
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (package_root, environment.get("PYTHONPATH"))))
        environment["DSH_PERMISSION_MODE"] = "danger-full-access"
        if self.config.dsh_home is not None:
            environment["DSH_HOME"] = str(self.config.dsh_home.expanduser().resolve())
        if self.config.skill_dir is not None:
            environment["DSH_BUNDLED_SKILL_DIR"] = str(self.config.skill_dir.expanduser().resolve())
        if self._budget.runtime_file is not None:
            runtime = json.loads(self._budget.runtime_file.read_text(encoding="utf-8"))
            environment["DSH_CONDUCTOR_RUNTIME"] = str(self._budget.runtime_file)
            environment["DSH_CONDUCTOR_SOCKET"] = runtime["tmux_socket"]
        try:
            command = self._command()
            self._budget.check()
            self._process = subprocess.Popen(command, cwd=self.config.workspace, env=environment,
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             bufsize=0, start_new_session=True)
            self._resource = register_process(self._process.pid, "dsh")
            if self._budget.runtime_file is not None:
                self._resource = register_process(self._process.pid, "dsh", self._budget.runtime_file)
            for name in ("stdin", "stdout", "stderr"):
                stream = getattr(self._process, name)
                os.set_blocking(stream.fileno(), False)
                if name != "stdin":
                    self._selector.register(stream, selectors.EVENT_READ, name)
        except OSError as exc:
            raise DshError(f"cannot start DSH: {exc}", code="dsh_start_failed") from exc
        return self

    def _protocol_error(self, message: str) -> None:
        if self._fatal is None:
            self._fatal = DshError(message, code="dsh_protocol_error", phase=self._budget.phase)
        raise self._fatal

    def _dispatch(self, frame: object) -> None:
        if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
            self._protocol_error("invalid JSON-RPC object or version")
            return
        request_id = frame.get("id")
        if "method" not in frame:
            if type(request_id) is not int or (("result" in frame) == ("error" in frame)):
                self._protocol_error("invalid JSON-RPC response")
                return
            pending = self._pending.get(request_id)
            if pending is None:
                return  # 已经超时的请求可能迟到；不能归给新请求。
            if "error" in frame:
                error = frame["error"]
                if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                    self._protocol_error("invalid JSON-RPC error")
                    return
                pending.error = f"{error.get('code')}: {error['message']}"
                failure = DshError(f"JSON-RPC request {pending.method} failed: {pending.error}",
                                   code="dsh_rpc_failed", phase=self._budget.phase,
                                   details={"rpc_method": pending.method})
                if self._fatal is None:
                    self._fatal = failure
                raise self._fatal
            else:
                pending.result = frame["result"]
            pending.ready.set()
            return
        method = frame.get("method")
        params = frame.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            self._protocol_error("invalid JSON-RPC notification")
            return
        if method == "session.event":
            event = params.get("event")
            if not isinstance(event, dict):
                self._protocol_error("session.event requires an event object")
                return
            self._events.append(event)
            if event.get("type") == "turn/end":
                data = event.get("data")
                reason = data.get("reason") if isinstance(data, dict) else None
                if not isinstance(reason, dict) or not isinstance(reason.get("kind"), str):
                    self._protocol_error("turn/end requires reason.kind")
                    return
                self._turn_end_reason = reason
            if self._event_hook is not None:
                self._event_hook.emit(event)
        elif method == "session.status":
            if not isinstance(params.get("status"), str):
                self._protocol_error("session.status requires a status string")
                return
            self._session_status = params["status"]

    def _read(self, stream: Any, name: str, budget: Budget) -> None:
        # 每批都检查预算，持续输出不能饿死取消路径。
        for _ in range(16):
            budget.check()
            try:
                block = os.read(stream.fileno(), 65536)
            except BlockingIOError:
                return
            if not block:
                try:
                    self._selector.unregister(stream)
                except KeyError:
                    pass
                tail = self._buffers[name]
                if tail:
                    self._line(bytes(tail), name)
                    tail.clear()
                return
            buffer = self._buffers[name]
            buffer.extend(block)
            while (index := buffer.find(b"\n")) >= 0:
                line = bytes(buffer[:index])
                del buffer[:index + 1]
                self._line(line, name)
                budget.check()
            if len(buffer) > 16 * 1024 * 1024:
                if name == "stdout":
                    self._protocol_error("JSON-RPC frame exceeds 16 MiB")
                del buffer[:-65536]

    def _line(self, line: bytes, name: str) -> None:
        if name == "stderr":
            self._stderr.append(line.decode("utf-8", errors="replace")[-65536:] + "\n")
        elif line.strip():
            try:
                self._dispatch(json.loads(line.decode("utf-8")))
            except (ValueError, UnicodeError, RecursionError) as exc:
                self._protocol_error(f"invalid JSON-RPC frame: {exc}")

    def _pump(self, budget: Budget, *, wait: bool = True) -> None:
        budget.check()
        # 在 poll 回收 leader 之前记录它的后代，即使主进程已成为 zombie。
        snapshot_group(self._resource)
        for key, _ in self._selector.select(min(POLL_SECONDS, budget.remaining()) if wait else 0):
            if key.data != "stdin":
                self._read(key.fileobj, key.data, budget)
        if self._fatal is not None:
            raise self._fatal

    def _exited_error(self, phase: str) -> DshError:
        assert self._process is not None
        return DshError(f"DSH process exited with code {self._process.returncode} before protocol completion: "
                        f"{self.stderr_tail().strip()[-1200:]}", code="dsh_process_exited", phase=phase,
                        details={"return_code": self._process.returncode})

    def _rpc(self, method: str, params: JsonObject | None, *, timeout_seconds: float,
             budget: Budget | None = None) -> object:
        operation = (budget or self._budget).limit(timeout_seconds)
        if self._process is None or self._process.stdin is None:
            raise DshError("DSH process is not running", code="dsh_process_exited", phase=operation.phase)
        request_id = self._next_id
        self._next_id += 1
        pending = _PendingRequest(method=method)
        self._pending[request_id] = pending
        frame: JsonObject = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        payload = memoryview((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
        stdin = self._process.stdin
        try:
            self._selector.register(stdin, selectors.EVENT_WRITE, "stdin")
            try:
                while payload:
                    operation.check()
                    try:
                        sent = os.write(stdin.fileno(), payload[:65536])
                        payload = payload[sent:]
                    except BlockingIOError:
                        self._pump(operation)
            finally:
                self._selector.unregister(stdin)
            while not pending.ready.is_set():
                self._pump(operation)
                if self._process.poll() is not None and not pending.ready.is_set():
                    self._pump(operation, wait=False)
                    if not pending.ready.is_set():
                        raise self._exited_error(operation.phase)
            if self._fatal is not None:
                raise self._fatal
            if pending.error is not None:
                raise DshError(f"JSON-RPC request {method} failed: {pending.error}", code="dsh_rpc_failed",
                               phase=operation.phase, details={"rpc_method": method})
            return pending.result
        except OperationError as exc:
            if isinstance(exc, DshError):
                raise
            raise DshError(str(exc), code=exc.code, phase=exc.phase,
                           details={**exc.details, "rpc_method": method}) from exc
        except (OSError, ValueError) as exc:
            raise DshError(f"cannot write JSON-RPC request {method}: {exc}", code="dsh_rpc_failed",
                           phase=operation.phase, details={"rpc_method": method}) from exc
        finally:
            self._pending.pop(request_id, None)

    def initialize(self) -> None:
        if self._initialized:
            return
        self._budget = self._budget.limit(float("inf"), phase="dsh_initialize")
        self._rpc("initialize", {"cwd": str(self.config.workspace.resolve()), "provider": self.config.provider,
                                 "model": self.config.model}, timeout_seconds=self.config.init_timeout_seconds)
        self._initialized = True

    def run(self, prompt: str, *, session_id: str, timeout_seconds: float,
            on_event: EventHook | None = None) -> RunResult:
        try:
            return self._run_turn(prompt, session_id=session_id, timeout_seconds=timeout_seconds, on_event=on_event)
        except DshError:
            raise
        except OperationError as exc:
            raise DshError(str(exc), code=exc.code, phase=exc.phase, details=exc.details) from exc

    def _run_turn(self, prompt: str, *, session_id: str, timeout_seconds: float,
                  on_event: EventHook | None = None) -> RunResult:
        started = time.monotonic()
        self._budget = (self.config.budget or Budget(float("inf"))).limit(timeout_seconds, phase="dsh_start")
        if self._process is None:
            self.start()
        self.initialize()
        self._events = []
        self._turn_end_reason = None
        self._session_status = None
        if self._event_hook is not None:
            self._event_hook.close(self._budget.deadline)
        self._event_hook = _HookDispatcher(on_event) if on_event is not None else None
        self._budget = self._budget.limit(float("inf"), phase="dsh_prompt")
        self._rpc("session/prompt", {"sessionId": session_id, "contentBlocks": [{"type": "text", "text": prompt}]},
                  timeout_seconds=60)
        self._budget = self._budget.limit(float("inf"), phase="dsh_run")
        while not (self._turn_end_reason is not None and self._session_status == "idle"):
            self._pump(self._budget)
            if self._process is not None and self._process.poll() is not None:
                self._pump(self._budget, wait=False)
                if not (self._turn_end_reason is not None and self._session_status == "idle"):
                    raise self._exited_error("dsh_run")
        self._budget.check()
        if self._fatal is not None:
            raise self._fatal
        kind = self._turn_end_reason.get("kind")
        status = "completed" if kind == "completed" else "interrupted" if kind == "interrupted" else "error"
        return RunResult(session_id, status, _last_assistant_text(self._events), self._turn_end_reason,
                         tuple(self._events), time.monotonic() - started, self.stderr_tail())

    def stderr_tail(self, lines: int = 30) -> str:
        return "".join(tuple(self._stderr)[-lines:])

    def close(self, *, budget: Budget | None = None, graceful: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        operation = (budget or Budget(float("inf"), "cleanup")).limit(self.config.shutdown_timeout_seconds, phase="cleanup")
        try:
            if self._process is None:
                return
            snapshot_group(self._resource)
            if graceful and self._fatal is None and self._process.poll() is None:
                try:
                    self._rpc("shutdown", None, timeout_seconds=min(self.config.shutdown_timeout_seconds,
                              max(0, operation.deadline - time.monotonic()) * 0.25), budget=operation)
                except (OperationError, OSError):
                    pass
            stop_process_group(self._resource, signal.SIGTERM)
            term_deadline = time.monotonic() + max(0, operation.deadline - time.monotonic()) * 0.25
            while snapshot_group(self._resource) and time.monotonic() < term_deadline:
                time.sleep(min(0.02, max(0, term_deadline - time.monotonic())))
            stop_process_group(self._resource, signal.SIGKILL)
            try:
                self._process.wait(timeout=max(0, operation.deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self.close_errors.append("DSH process did not exit before the cleanup deadline")
                reaper = threading.Thread(target=self._process.wait, name=f"reap-{self._process.pid}", daemon=True)
                operation.reapers.append(reaper)
                reaper.start()
        except (OSError, OperationError) as exc:
            self.close_errors.append(str(exc))
        finally:
            try:
                self._selector.close()
            except OSError as exc:
                self.close_errors.append(str(exc))
            if self._process is not None:
                for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError as exc:
                            self.close_errors.append(str(exc))
            if self._event_hook is not None:
                self._event_hook.close(operation.deadline)
                if self._event_hook.inflight:
                    self.close_errors.append("a DshClient event hook is still running; further delivery was stopped")

    def __enter__(self) -> DshClient:
        try:
            return self.start()
        except BaseException:
            self.close(graceful=False)
            raise

    def reap(self) -> None:
        if self._process is not None:
            self._process.poll()

    def __exit__(self, exc_type: object, *_: object) -> None:
        self.close(graceful=exc_type is None)
