#!/usr/bin/env python3
"""progress — 把 DSH 的实时事件流渲染成人类可读的进度。

DSH 通过 `session.event` 实时推送每一条持久化事实，可直接拿来显示进度：

    turn/start        一轮开始
    assistant/message 模型这一step的输出（含 text / reasoning / tool-call 块）
    tool/call         工具调用（含 arguments）
    tool/result       工具返回（含 isError）
    turn/end          本轮结局

**心跳是必需的**：单次工具调用可能阻塞几十秒（例如 `agent_task.py run`
要等整个下级 agent 跑完），这期间只有 tool/call 没有 tool/result。
心跳让"还活着"这件事一直可见，并显示正在等哪个工具。

输出到 **stderr**，保持 stdout 干净可管道。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Optional

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"


def _supports_color(stream) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


def _first_line(text: str, limit: int = 160) -> str:
    line = (text or "").strip().splitlines()
    first = line[0].strip() if line else ""
    return first[:limit] + ("…" if len(first) > limit else "")


def summarize_tool_call(name: str, arguments: object) -> str:
    """从工具的 arguments 里提取一句人类可读的摘要。"""
    args = arguments
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return _first_line(str(args), 100)
    if not isinstance(args, dict):
        return ""

    # bash：优先用模型写的 description，其次命令首行
    if name in ("bash", "pwsh"):
        desc = args.get("description")
        command = _first_line(str(args.get("command", "")), 110)
        if desc and command and desc.strip() != command:
            return f"{desc} · {command}"
        return str(desc or command)
    # 文件类：显示路径
    for key in ("file_path", "filePath", "path", "filename"):
        if args.get(key):
            return str(args[key])
    # skill：显示 skill 名
    if name == "skill":
        return str(args.get("name", ""))
    # 后台任务：显示 job id
    for key in ("job_id", "jobId", "session", "sessionId", "agent_id", "agentId"):
        if args.get(key):
            return f"{key}={args[key]}"
    # 兜底：第一个短字符串值
    for value in args.values():
        if isinstance(value, str) and len(value) <= 100:
            return value
    return ""


class ProgressReporter:
    """`on_event` 钩子 + 心跳线程。

    用法：
        reporter = ProgressReporter()
        reporter.start()
        try:
            client.run(prompt, on_event=reporter)
        finally:
            reporter.stop()
    """

    def __init__(
        self,
        stream=None,
        heartbeat_s: float = 10.0,
        verbose: bool = False,
        prefix: str = "dsh",
    ) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.heartbeat_s = heartbeat_s
        self.verbose = verbose
        self.prefix = prefix
        self.color = _supports_color(self.stream)
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._last_output = time.monotonic()
        self._current_tool: Optional[str] = None
        self._current_tool_at: float = 0.0
        self._tool_count = 0
        self._turn = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- 渲染 -------------------------------------------------------------

    def _c(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text

    def _emit(self, text: str) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._t0
            self.stream.write(f"{self._c(DIM, f'[{elapsed:7.1f}s]')} {self._c(BOLD, self.prefix)} {text}\n")
            self.stream.flush()
            self._last_output = time.monotonic()

    # -- 心跳 -------------------------------------------------------------

    def start(self) -> "ProgressReporter":
        self._t0 = time.monotonic()
        self._last_output = self._t0
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()
        return self

    def _heartbeat_loop(self) -> None:
        """长时间没有事件时，周期性报告"还活着 + 在等什么"。"""
        while not self._stop.wait(1.0):
            if time.monotonic() - self._last_output < self.heartbeat_s:
                continue
            elapsed = time.monotonic() - self._t0
            if self._current_tool:
                waited = time.monotonic() - self._current_tool_at
                note = f"等待 {self._c(CYAN, self._current_tool)} 返回（已 {waited:.0f}s）"
            else:
                note = "等待 DSH 下一步"
            with self._lock:
                self.stream.write(f"{self._c(DIM, f'[{elapsed:7.1f}s]')} {self._c(BOLD, self.prefix)} "
                                  f"{self._c(DIM, '··· ' + note)}\n")
                self.stream.flush()
                self._last_output = time.monotonic()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    # -- 事件钩子 ---------------------------------------------------------

    def __call__(self, event: dict) -> None:
        """作为 DshClient.run(on_event=...) 的回调。"""
        etype = event.get("type")
        data = event.get("data") or {}

        if etype == "turn/start":
            self._turn = data.get("turn", self._turn + 1)
            self._emit(self._c(BOLD, f"▶ 第 {self._turn} 轮开始"))

        elif etype == "assistant/message":
            self._on_assistant(data)

        elif etype == "tool/call":
            name = data.get("name", "?")
            self._tool_count += 1
            summary = summarize_tool_call(name, data.get("arguments"))
            self._current_tool = name
            self._current_tool_at = time.monotonic()
            detail = f" {self._c(DIM, summary)}" if summary else ""
            self._emit(f"{self._c(CYAN, '→ ' + name)}{detail}")

        elif etype == "tool/result":
            self._on_tool_result(data)

        elif etype == "turn/end":
            kind = (data.get("reason") or {}).get("kind", "?")
            paint = {"completed": GREEN, "interrupted": YELLOW}.get(kind, RED)
            self._emit(self._c(paint, f"■ 第 {data.get('turn', self._turn)} 轮结束：{kind}"))

    def _on_assistant(self, data: dict) -> None:
        content = ((data.get("message") or {}).get("content")) or []
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        if self.verbose:
            texts = [b.get("text", "") for b in content if b.get("type") == "reasoning"] + texts
        lines = [t for t in (_first_line(t) for t in texts) if t]
        if not lines:
            return
        usage = data.get("usage") or {}
        total = usage.get("totalTokens")
        # 用变量而不是嵌套 f-string：3.12 之前不允许内层用同种引号
        tokens = f"{self._c(DIM, f' ({total} tok)')}" if total else ""
        self._emit(f"{self._c(DIM, '💬')} {lines[0]}{tokens}")
        for extra in lines[1:]:
            self._emit(f"   {self._c(DIM, extra)}")

    def _on_tool_result(self, data: dict) -> None:
        self._current_tool = None
        blocks = ((data.get("message") or {}).get("content")) or []
        ok = True
        chars = 0
        preview = ""
        for block in blocks:
            if block.get("type") != "tool-result":
                continue
            ok = not block.get("isError", False)
            for inner in block.get("content") or []:
                if inner.get("type") == "text":
                    chars += len(inner.get("text", ""))
                    if not preview:
                        preview = _first_line(inner.get("text", ""), 90)
        mark = self._c(GREEN, "←") if ok else self._c(RED, "←")
        label = "ok" if ok else self._c(RED, "ERROR")
        tail = f" {self._c(DIM, preview)}" if preview else ""
        self._emit(f"{mark} {label} · {chars} 字符{tail}")

    def summary(self) -> str:
        elapsed = time.monotonic() - self._t0
        return f"{self._tool_count} 次工具调用，{elapsed:.1f}s"


# ---------------------------------------------------------------------------
# 下级 agent 的实时屏幕
# ---------------------------------------------------------------------------

# 子 agent 的 TUI 里这些行是装饰，不是内容
_CHROME = (
    "─", "━", "═", "╭", "╰", "│", ">_", "gpt-", "model:", "directory:", "permissions:",
    "Tip:", "esc to interrupt", "shift+tab", "bypass permissions",
)


def _is_chrome(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 2:
        return True
    if all(ch in "─━═│╭╮╰╯ " for ch in stripped):
        return True
    return any(marker in stripped for marker in _CHROME)


class AgentFollower:
    """轮询下级 agent 的 tmux 会话，把它屏幕上的**新内容**打印出来。

    为什么需要：DSH 常把长耗时的委派当作**后台任务**跑（bash 立即返回 job id），
    然后轮询 `job_output`。这段时间 DSH 侧只有心跳，看不到下级在干什么。
    而子会话名是调用方生成的，所以可以并行直播它的屏幕。

    只打印**从未出现过的行**并去重，避免 TUI 重绘导致刷屏。
    """

    def __init__(self, session: str, stream=None, interval_s: float = 2.0,
                 prefix: str = "claude", max_lines_per_poll: int = 4) -> None:
        self.session = session
        self.stream = stream if stream is not None else sys.stderr
        self.interval_s = interval_s
        self.prefix = prefix
        self.max_lines_per_poll = max_lines_per_poll
        self.color = _supports_color(self.stream)
        self._seen: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._announced = False

    def _capture(self) -> str:
        import subprocess
        try:
            proc = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", self.session, "-J"],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return ""
        return proc.stdout if proc.returncode == 0 else ""

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            screen = self._capture()
            if not screen:
                continue
            if not self._announced:
                self._announced = True
                with self._lock:
                    self.stream.write(f"{self._c(DIM, '  ┌ 下级会话 ' + self.session + ' 已启动，开始直播其屏幕')}\n")
                    self.stream.flush()
            fresh = []
            for line in screen.splitlines():
                text = line.rstrip()
                if not text.strip() or _is_chrome(text):
                    continue
                key = text.strip()
                if key in self._seen:
                    continue
                self._seen.add(key)
                fresh.append(key)
            for text in fresh[:self.max_lines_per_poll]:
                with self._lock:
                    self.stream.write(f"{self._c(DIM, '  │')} {self._c(CYAN, self.prefix)} {text[:150]}\n")
                    self.stream.flush()

    def _c(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text

    def start(self) -> "AgentFollower":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
