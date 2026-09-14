"""把 DSH 与 worker 的运行进展转换为 SDK 事件。"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

type JsonObject = dict[str, Any]
type EventCallback = Callable[["RunEvent"], None]

_STOP = object()


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


@dataclass(frozen=True, slots=True)
class RunEvent:
    """一次可实时消费的运行事件。"""

    elapsed_seconds: float
    source: str
    kind: str
    message: str
    raw: JsonObject | None = None

    def format(self) -> str:
        if self.source in {"claude", "codex"}:
            return f"[{self.elapsed_seconds:7.1f}s] {self.source} | {self.message}"
        return f"[{self.elapsed_seconds:7.1f}s] {self.source} {self.message}"

    def to_json(self) -> JsonObject:
        value: JsonObject = {
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "source": self.source,
            "kind": self.kind,
            "message": self.message,
        }
        if self.raw is not None:
            value["raw"] = self.raw
        return value


class ProgressReporter:
    """统一 DSH 事件、worker 屏幕与心跳，并交给调用方回调。"""

    def __init__(
        self,
        *,
        on_event: EventCallback | None = None,
        heartbeat_seconds: float = 10.0,
    ) -> None:
        self.on_event = on_event
        self.heartbeat_seconds = heartbeat_seconds
        self._started = time.monotonic()
        self._last_output = self._started
        self._current_tool: str | None = None
        self._current_tool_started = self._started
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._heartbeat_thread: threading.Thread | None = None
        self._dispatch_thread: threading.Thread | None = None
        self._events: queue.Queue[RunEvent | object] = queue.Queue()

    def start(self) -> "ProgressReporter":
        self._started = time.monotonic()
        self._last_output = self._started
        if self.on_event is not None:
            self._dispatch_thread = threading.Thread(
                target=self._dispatch_events,
                name="conductor-events",
                daemon=True,
            )
            self._dispatch_thread.start()
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat,
                name="dsh-heartbeat",
                daemon=True,
            )
            self._heartbeat_thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2)
        if self._dispatch_thread is not None:
            self._events.put(_STOP)
            self._dispatch_thread.join(timeout=2)

    def emit(
        self,
        *,
        source: str,
        kind: str,
        message: str,
        raw: JsonObject | None = None,
    ) -> None:
        if self.on_event is None:
            return
        with self._lock:
            event = RunEvent(
                elapsed_seconds=time.monotonic() - self._started,
                source=source,
                kind=kind,
                message=message,
                raw=raw,
            )
            self._last_output = time.monotonic()
        # DSH 协议线程只入队，用户回调的耗时不会阻塞后续 JSON-RPC 帧。
        self._events.put(event)

    def _dispatch_events(self) -> None:
        while True:
            event = self._events.get()
            if event is _STOP:
                return
            assert isinstance(event, RunEvent)
            try:
                self.on_event(event)
            except Exception:
                # 可观测性回调不能中断任务执行或破坏 DSH 协议线程。
                pass

    def worker_lines(self, agent: str, lines: Sequence[str]) -> None:
        for line in lines:
            self.emit(source=agent, kind="worker_output", message=line)

    def _heartbeat(self) -> None:
        while not self._stop.wait(0.5):
            if time.monotonic() - self._last_output < self.heartbeat_seconds:
                continue
            if self._current_tool:
                waited = time.monotonic() - self._current_tool_started
                self.emit(
                    source="dsh",
                    kind="heartbeat",
                    message=f"... waiting for {self._current_tool} ({waited:.0f}s)",
                )
            else:
                self.emit(
                    source="dsh",
                    kind="heartbeat",
                    message="... waiting for next protocol event",
                )

    def __call__(self, event: JsonObject) -> None:
        event_type = event.get("type")
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        if event_type == "turn/start":
            self.emit(source="dsh", kind="turn_start", message="turn started", raw=event)
        elif event_type == "assistant/message":
            message = data.get("message")
            message = message if isinstance(message, dict) else {}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    if text := _first_line(block.get("text")):
                        self.emit(
                            source="dsh",
                            kind="message",
                            message=f"message: {text}",
                            raw=event,
                        )
                        break
        elif event_type == "tool/call":
            name = str(data.get("name") or data.get("tool") or "tool")
            detail = summarize_tool(name, data.get("arguments"))
            self._current_tool = name
            self._current_tool_started = time.monotonic()
            self.emit(
                source="dsh",
                kind="tool_call",
                message=f"-> {name}" + (f": {detail}" if detail else ""),
                raw=event,
            )
        elif event_type == "tool/result":
            self.emit(
                source="dsh",
                kind="tool_result",
                message=f"<- {self._current_tool or 'tool'}",
                raw=event,
            )
            self._current_tool = None
        elif event_type == "turn/end":
            reason = data.get("reason")
            reason = reason if isinstance(reason, dict) else {}
            self.emit(
                source="dsh",
                kind="turn_end",
                message=f"turn ended: {reason.get('kind', 'unknown')}",
                raw=event,
            )
