"""采集 worker tmux 会话中可见的进展日志。"""

from __future__ import annotations

import difflib
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence

from .tmux import TmuxError, TmuxSession

type LogSink = Callable[[str, Sequence[str]], None]

_LOG_WRITE_LOCK = threading.Lock()

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LEADING_ACTIVITY = re.compile(r"^(\s*)[•◦](?=\s)")
_WORKING_TIMER = re.compile(r"\bWorking \((?:\d+m\s+)?\d+s(?=\s*[•·])")
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
    # 计时和 spinner 不是任务进展；归一化后只有实际文本变化才会再次输出。
    line = _LEADING_ACTIVITY.sub(r"\1*", line)
    line = _WORKING_TIMER.sub("Working (...", line)
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
    max_lines: int,
) -> tuple[str, ...]:
    """提取新出现或被替换的屏幕行，并限制单次输出规模。"""

    if max_lines <= 0 or not current:
        return ()
    matcher = difflib.SequenceMatcher(None, previous, current, autojunk=False)
    changed: list[str] = []
    for tag, _old_start, _old_end, new_start, new_end in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            changed.extend(current[new_start:new_end])

    # TUI 重绘可能在同一屏幕重复一行；单次事件只展示一次，保留最后的上下文。
    unique: list[str] = []
    seen: set[str] = set()
    for line in changed:
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
        interval_seconds: float = 2.0,
        history_lines: int = 200,
        max_lines_per_update: int = 12,
    ) -> None:
        self.session_name = session_name
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

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 2.0)
        # DSH 结束与 tmux 最后一次重绘可能紧邻，停止前再采样一次以减少尾部丢失。
        if not self._disabled:
            try:
                self.sample_once()
            except OSError:
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample_once()
            except OSError as exc:
                # 可观测性故障不能改变任务结果，只报告一次并停止采集。
                self._disabled = True
                self.sink(self.agent, (f"[tmux 日志采集已停止：{exc}]",))
                return
            self._stop.wait(self.interval_seconds)

    def sample_once(self) -> tuple[str, ...]:
        with self._sample_lock:
            return self._sample_once_locked()

    def _sample_once_locked(self) -> tuple[str, ...]:
        if self._disabled:
            return ()
        session = TmuxSession(self.session_name)
        try:
            if not session.exists():
                return ()
            current = normalize_screen(session.capture(history_lines=self.history_lines))
        except TmuxError:
            # 会话可能在 exists 与 capture 之间被 DSH 正常关闭。
            return ()
        if current == self._previous:
            return ()
        changed = changed_screen_lines(
            self._previous,
            current,
            max_lines=self.max_lines_per_update,
        )
        self._previous = current
        if not changed:
            return ()
        self._append(changed)
        self.sink(self.agent, changed)
        return changed

    def _append(self, lines: Sequence[str]) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).isoformat()
        # SDK 会在 agent 选择前监听两个候选会话，共享锁保证日志块不会交错。
        with _LOG_WRITE_LOCK, self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {self.agent}\n")
            for line in lines:
                handle.write(f"| {line}\n")
            handle.flush()
