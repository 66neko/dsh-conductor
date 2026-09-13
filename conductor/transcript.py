#!/usr/bin/env python3
"""transcript — 跟随编码 agent 自己写的 JSONL 转录，拿到**完整**日志。

为什么不能靠 tmux 抓屏拿完整日志：

    全屏 TUI 运行在**备用屏幕**（alternate screen）上。
    实测 `#{history_size}` 恒为 0，`capture-pane -S -N` 永远只能返回可见的那几十行。
    更本质的是：**TUI 发送的是"屏幕差分"，不是文本流**——它重绘而不是换行滚动，
    所以 tmux 根本没有内容可以存进滚动历史。

    （`tmux pipe-pane` 也不行：它给的是给终端回放用的原始字节流，
      剥掉 ANSI 后空格会丢失、内容重复、spinner 垃圾混入。）

但是 agent 自己会把**完整转录**写到磁盘，那才是权威日志：

    Claude Code : ~/.claude/projects/<cwd 把 / 换成 ->/<session-id>.jsonl
    Codex       : ~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<uuid>.jsonl

本模块增量跟随该文件：记录字节偏移、只发新行、保留不完整的尾行等待下次。
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"

# 单个文本块最多打印多少行，防止病态输出刷屏
MAX_BLOCK_LINES = 300


def _supports_color(stream) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


def project_slug(workspace: str) -> str:
    """Claude Code 的项目目录名：工作目录把 `/` 换成 `-`。"""
    return os.path.abspath(workspace).replace("/", "-")


def transcript_patterns(kind: str, workspace: str) -> tuple[list[str], list[str]]:
    """返回 (精确路径模式, 回退模式)。

    精确模式由工作目录推算（Claude Code 把 `/` 换成 `-`），
    回退模式用于推算不准的情况（软链、路径规范差异）。
    """
    home = Path.home()
    if kind == "claude":
        exact = [str(home / ".claude" / "projects" / project_slug(workspace) / "*.jsonl")]
        fallback = [str(home / ".claude" / "projects" / "*" / "*.jsonl")]
        return exact, fallback
    if kind == "codex":
        # Codex 按日期分目录，没有按工作目录分；只能按目录+新增判断
        return ([str(home / ".codex" / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl")], [])
    return ([], [])


def locate_transcript(
    kind: str,
    workspace: str,
    started_at: float,
    baseline: Optional[dict] = None,
) -> Optional[str]:
    """找到**本次会话**对应的转录文件。

    这里是整个模块最容易出错的地方：一个陈旧的转录会让日志显示上一次任务的内容，
    比什么都不显示更坏。所以判据是"相对启动时刻的**变化**"，不是"文件新不新"：

    - 精确目录里：命中启动时不存在的新文件，或已知文件**变大了**
    - 回退目录里：**只认启动时完全不存在的新文件**（更保守，避免误抓）

    @param baseline - 启动时已存在的 {路径: 字节数} 快照
    """
    baseline = baseline or {}
    exact, fallback = transcript_patterns(kind, workspace)

    def size_of(path: str) -> Optional[int]:
        try:
            return os.path.getsize(path)
        except OSError:
            return None

    # 1) 精确目录：新文件，或已知文件增长了
    newest: Optional[tuple] = None
    for pattern in exact:
        for path in glob.glob(pattern):
            size = size_of(path)
            if size is None:
                continue
            known = baseline.get(path)
            if known is None or size > known:
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if newest is None or mtime > newest[0]:
                    newest = (mtime, path)
    if newest is not None:
        return newest[1]

    # 2) 回退目录：严格只认启动后新出现的文件
    for pattern in fallback:
        for path in glob.glob(pattern):
            if path in baseline:
                continue  # 启动前就存在 → 绝不认
            size = size_of(path)
            if size is None:
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime + 1.0 < started_at:
                continue  # 明显早于本次会话
            if newest is None or mtime > newest[0]:
                newest = (mtime, path)
    return newest[1] if newest is not None else None


def snapshot_existing(kind: str, workspace: str) -> dict:
    """记录启动时已存在的转录文件及大小，作为"变化"判定的基线。"""
    exact, fallback = transcript_patterns(kind, workspace)
    snap: dict = {}
    for pattern in (*exact, *fallback):
        for path in glob.glob(pattern):
            try:
                snap[path] = os.path.getsize(path)
            except OSError:
                pass
    return snap


class AgentTranscript:
    """增量跟随 agent 的转录文件并把内容打印出来。

    只发新增的行；不完整的尾行留到下次（避免打印半截 JSON）。
    """

    def __init__(
        self,
        kind: str,
        workspace: str,
        stream=None,
        interval_s: float = 1.0,
        show_thinking: bool = False,
        prefix: Optional[str] = None,
    ) -> None:
        self.kind = kind
        self.workspace = os.path.abspath(workspace)
        self.stream = stream if stream is not None else __import__("sys").stderr
        self.interval_s = interval_s
        self.show_thinking = show_thinking
        self.prefix = prefix or kind
        self.color = _supports_color(self.stream)
        self._started_at = time.time()
        self._path: Optional[str] = None
        self._offset = 0
        self._buf = b""
        self._baseline: dict = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._records = 0
        self._announced = False

    def _c(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text

    def _write(self, text: str) -> None:
        with self._lock:
            for line in text.splitlines() or [""]:
                self.stream.write(f"{self._c(DIM, '  │')} {self._c(CYAN, self.prefix)} {line}\n")
            self.stream.flush()

    def _note(self, text: str) -> None:
        with self._lock:
            self.stream.write(f"{self._c(DIM, '  ┌ ' + text)}\n")
            self.stream.flush()

    # -- 主循环 -----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            if self._path is None:
                found = locate_transcript(self.kind, self.workspace, self._started_at, self._baseline)
                if found is None:
                    continue
                self._path = found
                self._note(f"跟随转录 {found}")
                self._announced = True
            self._drain()

    def _drain(self) -> None:
        try:
            size = os.path.getsize(self._path)
        except OSError:
            return
        if size <= self._offset:
            return
        try:
            with open(self._path, "rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read(size - self._offset)
        except OSError:
            return
        self._offset += len(chunk)
        data = self._buf + chunk
        lines = data.split(b"\n")
        self._buf = lines.pop()  # 最后一段可能不完整，留到下次
        for raw in lines:
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            self._records += 1
            try:
                self._emit(record)
            except Exception as exc:  # 单条记录异常不能中断跟随
                self._write(self._c(DIM, f"(转录记录解析跳过: {exc})"))

    # -- 按 agent 格式化 --------------------------------------------------

    def _emit(self, record: dict) -> None:
        if self.kind == "claude":
            self._emit_claude(record)
        elif self.kind == "codex":
            self._emit_codex(record)

    def _emit_claude(self, record: dict) -> None:
        rtype = record.get("type")
        if rtype not in ("assistant", "user"):
            return  # attachment / mode / title 等不是对话内容
        message = record.get("message") or {}
        role = message.get("role")
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                self._emit_text(block.get("text", ""), "assistant" if role == "assistant" else "user")
            elif btype == "thinking" and self.show_thinking:
                self._emit_text(block.get("thinking", ""), "thinking")
            elif btype == "tool_use":
                name = block.get("name", "?")
                args = block.get("input") or {}
                self._write(f"{self._c(YELLOW, '🔧 ' + name)} {self._brief(args)}")
            elif btype == "tool_result":
                self._emit_tool_result(block)

    def _emit_codex(self, record: dict) -> None:
        payload = record.get("payload") or {}
        if record.get("type") == "response_item" and payload.get("type") == "message":
            role = payload.get("role", "")
            for block in payload.get("content") or []:
                if isinstance(block, dict) and block.get("type") in ("output_text", "input_text", "text"):
                    self._emit_text(block.get("text", ""), role)
        elif record.get("type") == "event_msg" and payload.get("type") in ("agent_message", "task_complete"):
            text = payload.get("message") or payload.get("text") or ""
            if text:
                self._emit_text(str(text), "assistant")

    def _emit_text(self, text: str, role: str) -> None:
        text = (text or "").rstrip()
        if not text.strip():
            return
        marker = {"assistant": "💬", "user": "👤", "thinking": "🧠"}.get(role, "·")
        lines = text.splitlines()
        truncated = len(lines) > MAX_BLOCK_LINES
        if truncated:
            lines = lines[:MAX_BLOCK_LINES]
        self._write(f"{marker} {lines[0]}")
        for line in lines[1:]:
            self._write(f"   {line}")
        if truncated:
            self._write(self._c(DIM, f"   …（本块超过 {MAX_BLOCK_LINES} 行，已截断）"))

    def _emit_tool_result(self, block: dict) -> None:
        content = block.get("content")
        text = ""
        if isinstance(content, list):
            text = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        elif isinstance(content, str):
            text = content
        first = (text.strip().splitlines() or [""])[0][:120]
        mark = "❌" if block.get("is_error") else "↩"
        self._write(f"{self._c(DIM, mark + ' ' + (first or '(空)'))}")

    @staticmethod
    def _brief(args: object) -> str:
        if not isinstance(args, dict):
            return ""
        for key in ("command", "file_path", "path", "pattern", "description", "prompt"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().splitlines()[0][:110]
        return ""

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> "AgentTranscript":
        self._started_at = time.time()
        # 先拍快照：之后只认"新出现的文件"或"长大了的文件"，
        # 否则会误抓上一次任务留下的陈旧转录。
        self._baseline = snapshot_existing(self.kind, self.workspace)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        # 收尾再抽一次，尽量不丢最后的内容
        if self._path is not None:
            self._drain()
        if not self._announced:
            self._note(f"未找到 {self.kind} 转录文件（工作目录 {self.workspace}）")

    def summary(self) -> str:
        return f"{self._records} 条转录记录" + (f"（{os.path.basename(self._path)}）" if self._path else "")
