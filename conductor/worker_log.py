"""采集 worker tmux 会话中可见的进展日志。"""

from __future__ import annotations

import difflib
import re
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence

from .errors import OperationError
from .lifecycle import Budget
from .tmux import DEFAULT_CAPTURE_HISTORY_LINES, HISTORY_LIMIT, TmuxError, TmuxSession

type LogSink = Callable[[str, Sequence[str]], None]

DEFAULT_WORKER_LOG_INTERVAL_SECONDS = 10.0
_LOG_WRITE_LOCK = threading.Lock()

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LEADING_ACTIVITY = re.compile(r"^(\s*)[•◦](?=\s)")
_BRAILLE_SPINNER = re.compile(r"[\u2800-\u28ff]")
_DECORATIVE_LINE = re.compile(r"^[─━═┄┈╌╍┅┉\-_]{8,}$")
_RECEIPT_TOKEN = re.compile(r"\b[a-f0-9]{48}\b", re.IGNORECASE)
_HANDOFF_NOISE = (
    "dsh_conductor_handoff",
    "Complete the requested work autonomously.",
    "Do not treat this handoff as an acceptance test.",
    "As your final tool action, after all edits and checks are finished",
    "summary placeholder replaced by a short factual summary",
    "If an external blocker prevents completion",
    "Do not perform more work after writing the receipt.",
    "manager that your turn is ready for verification",
    "全部编辑与检查结束后",
    "写入回执后不要继续工作",
    "回执只表示可以交给 DSH",
)


def _is_handoff_noise(line: str) -> bool:
    stripped = line.strip()
    if _DECORATIVE_LINE.fullmatch(stripped):
        return True
    if any(fragment in stripped for fragment in _HANDOFF_NOISE):
        return True
    if " complete --receipt " in stripped:
        return True
    if "--token " in stripped and "--status " in stripped:
        return True
    if _RECEIPT_TOKEN.search(stripped):
        return True
    return bool(re.fullmatch(r"attempts/\d+/receipt\.json", stripped))


def _canonicalize_dynamic_status(line: str) -> str:
    # 仅减少 spinner 展示噪声；监督时钟独立统计原始活动。
    # 保留 Codex 的 Working 计时变化，供调用方和 DSH 观察 worker 活动。
    line = _LEADING_ACTIVITY.sub(r"\1*", line)
    return _BRAILLE_SPINNER.sub("*", line)


def normalize_screen(screen: str) -> tuple[str, ...]:
    """把 tmux 屏幕整理成适合比较和展示的非空文本行。"""

    lines: list[str] = []
    in_handoff = False
    for raw_line in screen.splitlines():
        line = _CONTROL_CHARACTERS.sub("", raw_line).rstrip()
        if "<dsh_conductor_handoff>" in line:
            in_handoff = True
        if in_handoff:
            if "</dsh_conductor_handoff>" in line:
                in_handoff = False
            continue
        if not line.strip() or _is_handoff_noise(line):
            continue
        lines.append(_canonicalize_dynamic_status(line))
    return tuple(lines)


def changed_screen_lines(
    previous: Sequence[str],
    current: Sequence[str],
    *,
    max_lines: int | None = None,
) -> tuple[str, ...]:
    """提取新增或替换的行；落盘默认保留全部，展示时才去重和限行。"""

    if (max_lines is not None and max_lines <= 0) or not current:
        return ()
    # 常见状态更新只改动尾部。先排除相同前后缀，避免大量重复历史行拖慢 diff。
    start = 0
    limit = min(len(previous), len(current))
    while start < limit and previous[start] == current[start]:
        start += 1
    old_end, new_end = len(previous), len(current)
    while old_end > start and new_end > start and previous[old_end - 1] == current[new_end - 1]:
        old_end -= 1
        new_end -= 1
    previous, current = previous[start:old_end], current[start:new_end]
    if len(previous) * len(current) > 1_000_000:
        # 大量重复行的精细 diff 为二次复杂度，可能拖慢采样及退出。
        # 此时保留变化区域的完整上下文，允许日志重复但不能截掉末尾输出。
        changed = tuple(current)
        return changed if max_lines is None else _display_lines(changed, max_lines=max_lines)
    matcher = difflib.SequenceMatcher(None, previous, current, autojunk=False)
    changed: list[str] = []
    for tag, _old_start, _old_end, new_start, new_end in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            changed.extend(current[new_start:new_end])

    if max_lines is None:
        return tuple(changed)
    return _display_lines(changed, max_lines=max_lines)


def _display_lines(lines: Sequence[str], *, max_lines: int) -> tuple[str, ...]:
    # TUI 重绘可能在同一屏幕重复一行；只限制实时展示，不能因此丢弃持久化日志。
    if max_lines <= 0:
        return ()
    unique: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(line)
    return tuple(unique[-max_lines:])


class WorkerLogFollower:
    """轮询一个确定的 tmux 会话，并输出可见屏幕的变化。"""

    def __init__(
        self,
        *,
        session_name: str,
        agent: str,
        log_file: Path,
        sink: LogSink,
        interval_seconds: float = DEFAULT_WORKER_LOG_INTERVAL_SECONDS,
        history_lines: int = DEFAULT_CAPTURE_HISTORY_LINES,
        max_lines_per_update: int = 12,
        socket_path: Path | None = None,
        budget: Budget | None = None,
    ) -> None:
        self.session_name = session_name
        self.socket_path = socket_path
        self.budget = budget
        self.agent = agent
        self.log_file = log_file
        self.sink = sink
        self.interval_seconds = interval_seconds
        self.history_lines = history_lines
        self.max_lines_per_update = max_lines_per_update
        self._previous: tuple[str, ...] = ()
        self._stop = threading.Event()
        self._sample_lock = threading.Lock()
        self._disabled = False
        self._thread: threading.Thread | None = None

    def start(self) -> "WorkerLogFollower":
        self._thread = threading.Thread(
            target=self._loop,
            name=f"worker-log-{self.agent}",
            daemon=True,
        )
        self._thread.start()
        return self

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, *, budget: Budget | None = None) -> None:
        operation = budget or Budget(time.monotonic() + 5, "cleanup")
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(0, min(0.2, operation.deadline - time.monotonic())))
        if not self._disabled:
            try:
                self.sample_once(history_lines=HISTORY_LIMIT, budget=operation)
            except (OSError, OperationError):
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample_once()
            except (OSError, OperationError) as exc:
                # 可观测性故障不能改变任务结果，只报告一次并停止采集。
                self._disabled = True
                self.sink(self.agent, (f"[tmux 日志采集已停止：{exc}]",))
                return
            self._stop.wait(self.interval_seconds)

    def sample_once(self, *, history_lines: int | None = None, budget: Budget | None = None) -> tuple[str, ...]:
        operation = budget or replace(self.budget or Budget(time.monotonic() + 30, "worker_run"), cancel_event=self._stop)
        while not self._sample_lock.acquire(timeout=min(0.1, operation.remaining())):
            operation.check()
        try:
            return self._sample_once_locked(history_lines=history_lines, budget=operation)
        finally:
            self._sample_lock.release()

    def _sample_once_locked(self, *, history_lines: int | None = None, budget: Budget | None = None) -> tuple[str, ...]:
        if self._disabled:
            return ()
        session = TmuxSession(self.session_name, socket_path=self.socket_path, budget=budget)
        try:
            if not session.exists():
                return ()
            current = normalize_screen(session.capture(
                history_lines=self.history_lines if history_lines is None else history_lines,
            ))
        except TmuxError:
            # 会话可能在 exists 与 capture 之间被 DSH 正常关闭。
            return ()
        if current == self._previous:
            return ()
        changed = changed_screen_lines(
            self._previous,
            current,
        )
        if not changed:
            self._previous = current
            return ()
        self._append(changed, budget=budget)
        self._previous = current
        displayed = _display_lines(changed, max_lines=self.max_lines_per_update)
        if displayed:
            self.sink(self.agent, displayed)
        return displayed

    def _append(self, lines: Sequence[str], *, budget: Budget | None = None) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).isoformat()
        # SDK 会在 agent 选择前监听两个候选会话，共享锁保证日志块不会交错。
        operation = budget or Budget(time.monotonic() + 30, "worker_run")
        while not _LOG_WRITE_LOCK.acquire(timeout=min(0.1, operation.remaining())):
            operation.check()
        try:
            operation.check()
            with self.log_file.open("a", encoding="utf-8") as handle:
                handle.write(f"[{timestamp}] {self.agent}\n")
                for line in lines:
                    operation.check()
                    handle.write(f"| {line}\n")
                handle.flush()
        finally:
            _LOG_WRITE_LOCK.release()
