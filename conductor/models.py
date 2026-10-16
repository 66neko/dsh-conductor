"""校验 conductor、DSH 与 worker agent 之间交换的持久化记录。"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Sequence

from .lifecycle import Budget

type JsonObject = dict[str, Any]


class RecordError(ValueError):
    """持久化记录不符合协议 schema。"""


class AgentKind(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"

    @property
    def skill_name(self) -> str:
        return {
            AgentKind.CLAUDE: "tmux-claude-code",
            AgentKind.CODEX: "tmux-codex",
        }[self]

    @property
    def script_name(self) -> str:
        return {
            AgentKind.CLAUDE: "claude_session.py",
            AgentKind.CODEX: "codex_session.py",
        }[self]


def _object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise RecordError(f"{label} must be a JSON object")
    return value


def _string(record: JsonObject, key: str, *, allow_empty: bool = False) -> str:
    value = record.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise RecordError(f"{key} must be a non-empty string")
    return value


def _integer(record: JsonObject, key: str, *, minimum: int = 0) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RecordError(f"{key} must be an integer >= {minimum}")
    return value


def _string_list(record: JsonObject, key: str, *, non_empty: bool = False) -> tuple[str, ...]:
    value = record.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise RecordError(f"{key} must be a list of non-empty strings")
    if non_empty and not value:
        raise RecordError(f"{key} must not be empty")
    return tuple(value)


def _read_text(path: Path, budget: Budget | None = None, *, preserve_newlines: bool = False) -> str:
    chunks = []
    # 非阻塞 open + fstat 拒绝 FIFO/设备，避免伪装成结果文件的管道无限等待。
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RecordError(f"record must be a regular file: {path}")
        handle = os.fdopen(fd, encoding="utf-8", newline="" if preserve_newlines else None)
    except BaseException:
        os.close(fd)
        raise
    with handle:
        while True:
            if budget is not None:
                budget.check()
            chunk = handle.read(65536)
            if not chunk:
                return "".join(chunks)
            chunks.append(chunk)


def read_json_object(path: Path, *, budget: Budget | None = None) -> JsonObject:
    try:
        value = json.loads(_read_text(path, budget))
        if budget is not None:
            budget.check()
    except OSError as exc:
        raise RecordError(f"cannot read {path}: {exc}") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RecordError(f"invalid JSON in {path}: {exc}") from exc
    return _object(value, str(path))


def worker_result_path(receipt_file: Path) -> Path:
    """结果固定存放在本轮 receipt 同目录，不能由终端输出重定向到其他轮次。"""
    return receipt_file.with_name("result.md")


def read_worker_result(receipt_file: Path, *, budget: Budget | None = None) -> str:
    path = worker_result_path(receipt_file)
    try:
        content = _read_text(path, budget)
    except (OSError, UnicodeError) as exc:
        raise RecordError(f"cannot read worker result {path}: {exc}") from exc
    if not content.strip():
        raise RecordError(f"worker result is empty: {path}")
    return content


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """DSH 的 agent 选择与任务摘要；验收目标直接取自用户 prompt。"""

    schema_version: int
    run_id: str
    agent: AgentKind
    agent_reason: str
    task_summary: str

    @classmethod
    def load(cls, path: Path, *, expected_run_id: str, budget: Budget | None = None) -> "ExecutionPlan":
        record = read_json_object(path, budget=budget)
        version = _integer(record, "schema_version", minimum=1)
        if version != 1:
            raise RecordError(f"unsupported plan schema_version: {version}")
        run_id = _string(record, "run_id")
        if run_id != expected_run_id:
            raise RecordError(f"plan run_id {run_id!r} does not match this run")
        try:
            agent = AgentKind(_string(record, "agent"))
        except ValueError as exc:
            raise RecordError("plan.agent is not supported") from exc
        return cls(
            schema_version=version,
            run_id=run_id,
            agent=agent,
            agent_reason=_string(record, "agent_reason"),
            task_summary=_string(record, "task_summary"),
        )

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "agent": self.agent.value,
            "agent_reason": self.agent_reason,
            "task_summary": self.task_summary,
        }


def registered_attempts(supervision_file: Path, *, run_id: str, agent: AgentKind, session: str,
                        budget: Budget | None = None) -> int:
    """读取监督记录登记的轮次；记录缺失或身份不符时视为未登记。"""
    try:
        record = read_json_object(supervision_file, budget=budget)
    except RecordError:
        return 0
    if (record.get("run_id"), record.get("agent"), record.get("session")) != (run_id, agent.value, session):
        return 0
    number = record.get("attempt")
    return number if type(number) is int and number > 0 else 0


VERDICT_SCHEMA_VERSION = 3


@dataclass(frozen=True, slots=True)
class Verdict:
    """DSH 只给出结论；run、agent 与轮次由 SDK 从 plan 和落盘回执推导。"""

    schema_version: int
    run_id: str
    status: str
    agent: AgentKind
    attempts: int
    summary: str
    remaining_issues: tuple[str, ...]

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        plan: ExecutionPlan,
        expected_receipts: Sequence[tuple[Path, str]],
        registered_attempts: int = 0,
        budget: Budget | None = None,
    ) -> "Verdict":
        record = read_json_object(path, budget=budget)
        version = _integer(record, "schema_version", minimum=1)
        if version != VERDICT_SCHEMA_VERSION:
            raise RecordError(f"unsupported verdict schema_version: {version}; expected {VERDICT_SCHEMA_VERSION}")
        status = _string(record, "status")
        if status not in {"accepted", "rejected"}:
            raise RecordError("verdict.status must be accepted or rejected")
        summary = _string(record, "summary")
        issues = _string_list(record, "remaining_issues")
        if status == "accepted" and issues:
            raise RecordError("accepted verdict cannot contain remaining issues")
        if status == "rejected" and not issues:
            raise RecordError("rejected verdict requires at least one remaining issue")

        # 轮次取监督登记轮次与最后一个已落盘回执的较大者，不依赖 DSH 自报。
        attempts = min(max(registered_attempts, 0), len(expected_receipts))
        for number, (receipt_path, _token) in enumerate(expected_receipts, start=1):
            if budget is not None:
                budget.check()
            if receipt_path.exists():
                attempts = max(attempts, number)
        if status == "accepted":
            if attempts == 0:
                raise RecordError("accepted verdict requires a worker receipt")
            receipt_path, token = expected_receipts[attempts - 1]
            receipt = WorkerReceipt.load(receipt_path, expected_token=token, budget=budget)
            if receipt.status != "ready_for_verification":
                raise RecordError("accepted verdict requires a ready_for_verification final receipt")

        return cls(
            schema_version=version,
            run_id=plan.run_id,
            status=status,
            agent=plan.agent,
            attempts=attempts,
            summary=summary,
            remaining_issues=issues,
        )

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status,
            "agent": self.agent.value,
            "attempts": self.attempts,
            "summary": self.summary,
            "remaining_issues": list(self.remaining_issues),
        }


@dataclass(frozen=True, slots=True)
class WorkerReceipt:
    token: str
    status: str
    summary: str

    @classmethod
    def load(cls, path: Path, *, expected_token: str, budget: Budget | None = None) -> "WorkerReceipt":
        # token 将回执绑定到唯一 attempt，避免旧文件或其他会话被误认为本轮完成。
        record = read_json_object(path, budget=budget)
        if _integer(record, "schema_version", minimum=1) != 1:
            raise RecordError("unsupported worker receipt schema")
        token = _string(record, "token")
        if token != expected_token:
            raise RecordError("worker receipt token does not match this attempt")
        status = _string(record, "status")
        if status not in {"ready_for_verification", "blocked"}:
            raise RecordError("worker receipt has an unsupported status")
        # 回执是交接信号；完整结果必须已经落盘，包括 blocked 时的原因和已完成工作。
        read_worker_result(path, budget=budget)
        return cls(token=token, status=status, summary=_string(record, "summary"))

    def to_json(self) -> JsonObject:
        return {
            "schema_version": 1,
            "token": self.token,
            "status": self.status,
            "summary": self.summary,
        }
