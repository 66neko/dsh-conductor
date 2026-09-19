"""可取消的等待、截止时间和工作区互斥；不修改宿主信号或环境。"""

from __future__ import annotations

import fcntl
import math
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, Sequence

from .errors import OperationError

POLL_SECONDS = 0.1


def positive_seconds(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number greater than zero")


@dataclass(frozen=True, slots=True)
class Budget:
    deadline: float
    phase: str = "preparation"
    cancel_event: threading.Event | None = None
    scope: str = "total"
    stop_file: Path | None = None
    runtime_file: Path | None = None
    reapers: list[threading.Thread] = field(default_factory=list, compare=False, repr=False)

    def check(self) -> None:
        if ((self.cancel_event is not None and self.cancel_event.is_set())
                or (self.stop_file is not None and self.stop_file.exists())):
            raise OperationError("run cancelled", code="cancelled", phase=self.phase)
        if time.monotonic() >= self.deadline:
            raise OperationError(f"{self.phase} exceeded its deadline", code="timeout", phase=self.phase,
                                 details={"timeout_scope": self.scope})

    def remaining(self) -> float:
        self.check()
        return max(0.0, self.deadline - time.monotonic())

    def limit(self, seconds: float, *, phase: str | None = None) -> Budget:
        deadline = time.monotonic() + seconds
        return replace(self, deadline=min(self.deadline, deadline), phase=phase or self.phase,
                       scope="stage" if deadline < self.deadline else self.scope)

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            self.check()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            delay = min(POLL_SECONDS, remaining, self.remaining())
            if self.cancel_event is None:
                time.sleep(delay)
            else:
                self.cancel_event.wait(delay)


class RunContext:
    def __init__(self, timeout_seconds: float, cleanup_seconds: float,
                 cancel_event: threading.Event | None = None) -> None:
        self.started = time.monotonic()
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.total_deadline = self.started + timeout_seconds
        self.execution_deadline = self.total_deadline - min(cleanup_seconds, timeout_seconds * 0.1)
        self.cleanup_seconds = cleanup_seconds
        self.phase = "validation"
        self.run_id: str | None = None
        self.state_directory: Path | None = None
        self.first_error: BaseException | None = None
        self.reapers: list[threading.Thread] = []

    def budget(self, phase: str | None = None) -> Budget:
        if phase is not None:
            self.phase = phase
        return Budget(self.execution_deadline, self.phase, self.cancel_event,
                      runtime_file=self.state_directory / "runtime.json" if self.state_directory else None,
                      reapers=self.reapers)

    def cleanup_budget(self) -> Budget:
        # 清理不再使用取消事件，也不把执行阶段的停止标记当成清理停止信号。
        return Budget(min(self.total_deadline, time.monotonic() + self.cleanup_seconds), "cleanup", reapers=self.reapers)


def acquire_lock(handle: IO, budget: Budget, *, immediate: bool = False) -> None:
    while True:
        budget.check()
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if immediate:
                raise OperationError("another run is using this workspace", code="workspace_busy",
                                     phase=budget.phase) from None
            budget.sleep(POLL_SECONDS)


def run_command(arguments: Sequence[str], *, budget: Budget | None = None,
                timeout_seconds: float = 20, input_text: str | None = None,
                cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """communicate 的每次有限等待共用预算；异常路径不等待管道 EOF。"""
    operation = (budget or Budget(float("inf"))).limit(timeout_seconds)
    operation.check()
    process = subprocess.Popen(arguments, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                               start_new_session=True, bufsize=0)
    from .processes import register_process, snapshot_group, stop_process_group
    record = register_process(process.pid, "command")
    try:
        if operation.runtime_file is not None:
            record = register_process(process.pid, "command", operation.runtime_file)
        payload = input_text.encode("utf-8") if input_text is not None else None
        while True:
            operation.check()
            snapshot_group(record)
            try:
                stdout, stderr = process.communicate(payload, timeout=min(POLL_SECONDS, operation.remaining()))
                operation.check()
                return subprocess.CompletedProcess(arguments, process.returncode,
                                                   stdout.decode("utf-8", errors="replace"),
                                                   stderr.decode("utf-8", errors="replace"))
            except subprocess.TimeoutExpired:
                payload = None
    finally:
        # tmux/探测命令本身不应有持久子进程；独立 tmux server 另外登记。
        stop_process_group(record, signal.SIGKILL)
        try:
            process.wait(timeout=max(0, min(0.1, operation.deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            # 截止点已到，不能给主线程另加等待。保留 Popen 所有权直到内核回收，
            # 同时登记 reaper，SDK 会在清理报告中列出仍未结束的受管线程。
            reaper = threading.Thread(target=process.wait, name=f"reap-{process.pid}", daemon=True)
            operation.reapers.append(reaper)
            reaper.start()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        path = record.get("record_file")
        if path and not snapshot_group(record):
            Path(path).unlink(missing_ok=True)
