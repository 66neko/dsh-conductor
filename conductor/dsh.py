#!/usr/bin/env python3
"""dsh_client — 用标准库从 Python 驱动 DSH 运行时（`dsh --profile sdk`）。

协议：newline-delimited JSON-RPC 2.0 over stdio。

    client->server : initialize / session/prompt / shutdown
    server->client : session.event / session.status / subagent.started / subagent.finished

完成判定用的是**协议事实**，不是猜：
  某个 turn 的 `turn/end` 事件给出结局（reason.kind），随后的
  `session.status: idle` 表示该 agent 已收敛。

只用标准库，无第三方依赖。

用法：
    from dsh_client import DshClient, DshConfig
    with DshClient(DshConfig(workspace="/path/to/proj")) as dsh:
        result = dsh.run("你的任务")
        print(result.final_text, result.status)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


class DshError(RuntimeError):
    """DSH 进程启动失败、协议错误或运行超时。"""


def resolve_dsh_bin(explicit: Optional[str] = None) -> str:
    """定位 `dsh` 入口，优先级：显式参数 > `$DSH_BIN` > PATH > 源码 checkout 兜底。

    @param explicit - `--dsh-bin` 传入的值
    @returns dsh 入口；`.js`/`.mjs` 结尾表示需用 node 启动的模块，否则当命令名
    @throws DshError - 全部落空时，并提示 DSH 是什么、去哪找
    """
    for candidate in (explicit, os.environ.get("DSH_BIN")):
        if candidate:
            return candidate
    found = shutil.which("dsh")
    if found:
        return found
    for guess in (
        "/home/yu/workspace/deepseek-harness/apps/cli/lib/bin.js",
        os.path.expanduser("~/deepseek-harness/apps/cli/lib/bin.js"),
    ):
        if os.path.exists(guess):
            return guess
    raise DshError(
        "找不到 dsh 入口：请用 --dsh-bin 指定，或设置 $DSH_BIN，或把 dsh 装到 PATH。"
        "DSH（DeepSeek Harness）见 https://github.com/deepseek-ai/deepseek-harness"
    )


@dataclass
class DshConfig:
    """启动一个 DSH SDK 运行时的全部参数。"""

    # dsh 入口：`.js`/`.mjs` 走 node，其他当命令名；None 表示自动解析
    dsh_bin: Optional[str] = None
    profile: str = "sdk"
    # 工作目录：既是子进程 cwd，也是 session 记录的工作区
    workspace: str = field(default_factory=os.getcwd)
    provider: str = "deepseek-official"
    model: str = "deepseek-flash"
    # DSH 侧权限模式。danger-full-access ⇒ 审批 never + 沙箱放开。
    # 本机 confining 沙箱没有可用后端（无 bwrap / landlock），
    # 所以必须 danger-full-access，否则 DSH 的 bash 会被整体拒绝。
    permission_mode: str = "danger-full-access"
    dsh_home: Optional[str] = None
    init_timeout_ms: int = 30_000
    shutdown_timeout_ms: int = 5_000
    extra_env: Optional[dict] = None


@dataclass
class ToolCall:
    """从 session 事件里提取的一次工具调用（用于观察 DSH 在做什么）。"""

    name: str
    at_ms: int


@dataclass
class RunResult:
    """一次 prompt 的完整结果。"""

    session_id: str
    status: str  # completed | error | interrupted | timeout
    final_text: str
    turn_end_reason: Optional[dict]
    tool_calls: list
    event_count: int
    elapsed_ms: int
    stderr_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "completed"


def _last_assistant_text(events: list) -> str:
    """取最后一条非空 assistant/message 的文本。"""
    for event in reversed(events):
        if event.get("type") != "assistant/message":
            continue
        content = (event.get("data") or {}).get("message", {}).get("content") or []
        text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            return text
    return ""


class DshClient:
    """一个 DSH SDK 运行时子进程。"""

    def __init__(self, config: Optional[DshConfig] = None) -> None:
        self.config = config or DshConfig()
        self._proc: Optional[subprocess.Popen] = None
        self._next_id = 1
        self._pending: dict = {}
        self._lock = threading.Lock()
        self._stderr_lines: list = []
        self._events: list = []
        self._tool_calls: list = []
        self._turn_end: Optional[dict] = None
        self._saw_turn_end = False
        self._idle = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._closed = False

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> "DshClient":
        config = self.config
        dsh_bin = resolve_dsh_bin(config.dsh_bin)
        if dsh_bin.endswith((".js", ".mjs")):
            argv = ["node", dsh_bin]
        else:
            argv = [dsh_bin]
        argv += ["--profile", config.profile]

        env = dict(os.environ)
        env["DSH_PERMISSION_MODE"] = config.permission_mode
        if config.dsh_home:
            env["DSH_HOME"] = config.dsh_home
        if config.extra_env:
            env.update(config.extra_env)

        self._proc = subprocess.Popen(
            argv,
            cwd=config.workspace,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # 行缓冲：协议是按行分帧的
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # stderr 必须持续抽干，否则管道写满会把子进程卡死
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        return self

    def _read_loop(self) -> None:
        """读取 stdout 的每一帧并分派。stdout 是纯协议，诊断走 stderr。"""
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue  # 非协议行直接忽略
            self._dispatch(frame)
        # stdout 结束：唤醒所有等待者
        with self._lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for waiter in waiters:
            waiter["error"] = "DSH 运行时已退出"
            waiter["event"].set()
        self._idle.set()

    def _dispatch(self, frame: dict) -> None:
        # 响应
        if frame.get("id") is not None and frame.get("method") is None:
            with self._lock:
                waiter = self._pending.pop(frame["id"], None)
            if waiter is None:
                return
            if "error" in frame:
                waiter["error"] = f"{frame['error'].get('code')}: {frame['error'].get('message')}"
            else:
                waiter["result"] = frame.get("result")
            waiter["event"].set()
            return

        # 通知
        params = frame.get("params") or {}
        method = frame.get("method")
        if method == "session.event":
            event = params.get("event") or {}
            hook = getattr(self, "_on_event", None)
            if hook is not None:
                try:
                    hook(event)  # 观察者异常不能影响协议解析
                except Exception:
                    pass
            self._events.append(event)
            if event.get("type") == "turn/end":
                self._saw_turn_end = True
                self._turn_end = (event.get("data") or {}).get("reason")
            elif event.get("type") == "tool/call":
                name = (event.get("data") or {}).get("name") or (event.get("data") or {}).get("tool")
                if name:
                    self._tool_calls.append(ToolCall(str(name), int(time.time() * 1000)))
        elif method == "session.status":
            if params.get("status") == "idle" and self._saw_turn_end:
                self._idle.set()

    def _rpc(self, method: str, params, timeout_s: float = 60.0):
        if self._proc is None or self._proc.stdin is None:
            raise DshError("DSH 运行时未启动")
        with self._lock:
            rpc_id = self._next_id
            self._next_id += 1
            waiter = {"event": threading.Event(), "result": None, "error": None}
            self._pending[rpc_id] = waiter
        payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise DshError(f"写入 DSH 失败（运行时可能已退出）：{exc}") from exc
        if not waiter["event"].wait(timeout_s):
            raise DshError(f"{method} 在 {timeout_s}s 内没有响应")
        if waiter["error"]:
            raise DshError(f"{method} 失败：{waiter['error']}")
        return waiter["result"]

    def initialize(self) -> dict:
        """握手。服务端会在此校验 provider/model 路由。"""
        return self._rpc("initialize", {
            "cwd": os.path.abspath(self.config.workspace),
            "provider": self.config.provider,
            "model": self.config.model,
        }, timeout_s=self.config.init_timeout_ms / 1000)

    # -- 运行一次 ---------------------------------------------------------

    def run(
        self,
        text: str,
        session_id: str = "dsh-session",
        timeout_ms: int = 900_000,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> RunResult:
        """发送一条用户消息，等到 agent 收敛，返回结构化结果。"""
        if self._proc is None:
            self.start()
        self.initialize()

        # 每次运行前重置观察状态
        self._events = []
        self._tool_calls = []
        self._turn_end = None
        self._saw_turn_end = False
        self._idle.clear()

        # 观察者通过属性注册，而不是替换 _dispatch——替换会在多次 run 时层层嵌套。
        self._on_event = on_event

        started = time.monotonic()
        # 回执只表示"已入队"，绝不表示"已回答"
        self._rpc("session/prompt", {
            "sessionId": session_id,
            "contentBlocks": [{"type": "text", "text": text}],
        })

        if not self._idle.wait(timeout_ms / 1000):
            return RunResult(
                session_id=session_id, status="timeout", final_text=_last_assistant_text(self._events),
                turn_end_reason=self._turn_end, tool_calls=list(self._tool_calls),
                event_count=len(self._events), elapsed_ms=int((time.monotonic() - started) * 1000),
                stderr_tail=self.stderr_tail(),
            )

        kind = (self._turn_end or {}).get("kind")
        status = {"completed": "completed", "interrupted": "interrupted"}.get(kind, "error" if self._saw_turn_end else "error")

        return RunResult(
            session_id=session_id,
            status=status,
            final_text=_last_assistant_text(self._events),
            turn_end_reason=self._turn_end,
            tool_calls=list(self._tool_calls),
            event_count=len(self._events),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            stderr_tail=self.stderr_tail(),
        )

    # -- 收尾 -------------------------------------------------------------

    def stderr_tail(self, lines: int = 20) -> str:
        if self._proc is None or self._proc.stderr is None:
            return ""
        return "".join(self._stderr_lines[-lines:])

    def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        for line in self._proc.stderr:
            self._stderr_lines.append(line)

    def close(self) -> None:
        """优雅关闭：shutdown → stdin EOF → SIGTERM → SIGKILL。"""
        if self._closed or self._proc is None:
            return
        self._closed = True
        try:
            self._rpc("shutdown", None, timeout_s=self.config.shutdown_timeout_ms / 1000)
        except Exception:
            pass
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=self.config.shutdown_timeout_ms / 1000)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def __enter__(self) -> "DshClient":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()
