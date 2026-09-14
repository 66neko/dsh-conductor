"""将 DSH 协议事件输出到 stderr，不影响 stdout 的机器协议。"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any, TextIO


def _first_line(value: object, limit: int = 180) -> str:
    lines = str(value or "").strip().splitlines()
    text = lines[0] if lines else ""
    return text[:limit] + ("..." if len(text) > limit else "")


def summarize_tool(name: str, arguments: object) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return _first_line(arguments)
    if not isinstance(arguments, dict):
        return ""
    if name in {"bash", "pwsh"}:
        return _first_line(arguments.get("description") or arguments.get("command"))
    for key in ("name", "file_path", "path", "job_id", "sessionId"):
        if value := arguments.get(key):
            return _first_line(value)
    return ""


class ProgressReporter:
    def __init__(self, *, heartbeat_seconds: float = 10.0, stream: TextIO | None = None) -> None:
        self.heartbeat_seconds = heartbeat_seconds
        self.stream = stream or sys.stderr
        self._started = time.monotonic()
        self._last_output = self._started
        self._current_tool: str | None = None
        self._current_tool_started = self._started
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> "ProgressReporter":
        self._started = time.monotonic()
        self._last_output = self._started
        self._thread = threading.Thread(target=self._heartbeat, name="dsh-heartbeat", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _write(self, message: str) -> None:
        # 心跳线程与协议线程共享 stderr，锁保证单行日志不会互相穿插。
        with self._lock:
            elapsed = time.monotonic() - self._started
            self.stream.write(f"[{elapsed:7.1f}s] dsh {message}\n")
            self.stream.flush()
            self._last_output = time.monotonic()

    def _heartbeat(self) -> None:
        while not self._stop.wait(0.5):
            if time.monotonic() - self._last_output < self.heartbeat_seconds:
                continue
            if self._current_tool:
                waited = time.monotonic() - self._current_tool_started
                self._write(f"... waiting for {self._current_tool} ({waited:.0f}s)")
            else:
                self._write("... waiting for next protocol event")

    def __call__(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        if event_type == "turn/start":
            self._write("turn started")
        elif event_type == "assistant/message":
            message = data.get("message")
            message = message if isinstance(message, dict) else {}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    if text := _first_line(block.get("text")):
                        self._write(f"message: {text}")
                        break
        elif event_type == "tool/call":
            name = str(data.get("name") or data.get("tool") or "tool")
            detail = summarize_tool(name, data.get("arguments"))
            self._current_tool = name
            self._current_tool_started = time.monotonic()
            self._write(f"-> {name}" + (f": {detail}" if detail else ""))
        elif event_type == "tool/result":
            self._write(f"<- {self._current_tool or 'tool'}")
            self._current_tool = None
        elif event_type == "turn/end":
            reason = data.get("reason")
            reason = reason if isinstance(reason, dict) else {}
            self._write(f"turn ended: {reason.get('kind', 'unknown')}")
