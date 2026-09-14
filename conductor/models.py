"""校验 conductor、DSH 与 worker agent 之间交换的持久化记录。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Sequence

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


def read_json_object(path: Path) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RecordError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RecordError(f"invalid JSON in {path}: {exc}") from exc
    return _object(value, str(path))


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    id: str
    description: str

    @classmethod
    def from_json(cls, value: object) -> "AcceptanceCriterion":
        record = _object(value, "acceptance criterion")
        criterion_id = _string(record, "id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", criterion_id):
            raise RecordError("criterion id has an unsupported format")
        return cls(id=criterion_id, description=_string(record, "description"))

    def to_json(self) -> JsonObject:
        return {"id": self.id, "description": self.description}


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    schema_version: int
    run_id: str
    agent: AgentKind
    agent_reason: str
    task_summary: str
    implementation_steps: tuple[str, ...]
    acceptance_criteria: tuple[AcceptanceCriterion, ...]

    @classmethod
    def load(cls, path: Path, *, expected_run_id: str) -> "ExecutionPlan":
        record = read_json_object(path)
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
        raw_criteria = record.get("acceptance_criteria")
        if not isinstance(raw_criteria, list) or not raw_criteria:
            raise RecordError("acceptance_criteria must be a non-empty list")
        criteria = tuple(AcceptanceCriterion.from_json(item) for item in raw_criteria)
        ids = [criterion.id for criterion in criteria]
        if len(ids) != len(set(ids)):
            raise RecordError("acceptance criterion ids must be unique")
        return cls(
            schema_version=version,
            run_id=run_id,
            agent=agent,
            agent_reason=_string(record, "agent_reason"),
            task_summary=_string(record, "task_summary"),
            implementation_steps=_string_list(record, "implementation_steps", non_empty=True),
            acceptance_criteria=criteria,
        )

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "agent": self.agent.value,
            "agent_reason": self.agent_reason,
            "task_summary": self.task_summary,
            "implementation_steps": list(self.implementation_steps),
            "acceptance_criteria": [item.to_json() for item in self.acceptance_criteria],
        }


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    criterion_id: str
    criterion: str
    method: str
    evidence: str
    passed: bool

    @classmethod
    def from_json(cls, value: object) -> "VerificationCheck":
        record = _object(value, "check")
        passed = record.get("passed")
        if not isinstance(passed, bool):
            raise RecordError("check.passed must be a boolean")
        return cls(
            criterion_id=_string(record, "criterion_id"),
            criterion=_string(record, "criterion"),
            method=_string(record, "method"),
            evidence=_string(record, "evidence"),
            passed=passed,
        )

    def to_json(self) -> JsonObject:
        return {
            "criterion_id": self.criterion_id,
            "criterion": self.criterion,
            "method": self.method,
            "evidence": self.evidence,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class Verdict:
    schema_version: int
    run_id: str
    status: str
    agent: AgentKind
    attempts: int
    artifacts: tuple[str, ...]
    checks: tuple[VerificationCheck, ...]
    summary: str
    remaining_issues: tuple[str, ...]

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        plan: ExecutionPlan,
        max_attempts: int,
        workspace: Path,
        expected_receipts: Sequence[tuple[Path, str]],
    ) -> "Verdict":
        record = read_json_object(path)
        version = _integer(record, "schema_version", minimum=1)
        if version != 2:
            raise RecordError(f"unsupported verdict schema_version: {version}")
        run_id = _string(record, "run_id")
        if run_id != plan.run_id:
            raise RecordError(f"verdict run_id {run_id!r} does not match the plan")
        try:
            agent = AgentKind(_string(record, "agent"))
        except ValueError as exc:
            raise RecordError("verdict.agent is not supported") from exc
        if agent is not plan.agent:
            raise RecordError(f"verdict agent {agent.value!r} does not match the plan")
        status = _string(record, "status")
        if status not in {"accepted", "rejected"}:
            raise RecordError("verdict.status must be accepted or rejected")
        attempts = _integer(record, "attempts", minimum=0)
        if attempts > max_attempts:
            raise RecordError("verdict.attempts exceeds max_attempts")
        if status == "accepted" and attempts == 0:
            raise RecordError("accepted verdict requires at least one worker attempt")

        raw_artifacts = record.get("artifacts")
        if not isinstance(raw_artifacts, list) or not all(isinstance(item, str) for item in raw_artifacts):
            raise RecordError("verdict.artifacts must be a list of strings")
        artifacts: list[str] = []
        root = workspace.resolve()
        for item in raw_artifacts:
            relative = Path(item)
            if relative.is_absolute() or relative == Path("."):
                raise RecordError(f"artifact must be a relative path: {item!r}")
            resolved = (root / relative).resolve(strict=False)
            if not resolved.is_relative_to(root):
                raise RecordError(f"artifact escapes workspace: {item!r}")
            if status == "accepted" and not resolved.exists():
                raise RecordError(f"accepted artifact does not exist: {item!r}")
            artifacts.append(item)

        raw_checks = record.get("checks")
        if not isinstance(raw_checks, list):
            raise RecordError("verdict.checks must be a list")
        checks = tuple(VerificationCheck.from_json(item) for item in raw_checks)
        planned_ids = {criterion.id for criterion in plan.acceptance_criteria}
        check_ids = [check.criterion_id for check in checks]
        if len(check_ids) != len(set(check_ids)):
            raise RecordError("verdict criterion ids must be unique")
        if set(check_ids) != planned_ids:
            raise RecordError("verdict checks must cover every planned acceptance criterion exactly once")
        if status == "accepted" and not all(check.passed for check in checks):
            raise RecordError("accepted verdict requires all checks to pass")

        issues = _string_list(record, "remaining_issues")
        if status == "accepted" and issues:
            raise RecordError("accepted verdict cannot contain remaining issues")
        if status == "rejected" and not issues:
            raise RecordError("rejected verdict requires at least one remaining issue")

        if status == "accepted":
            # accepted 必须能追溯到所选 agent 的每一次真实交接。
            if len(expected_receipts) < attempts:
                raise RecordError("accepted verdict has no receipt definition for every attempt")
            receipts = [
                WorkerReceipt.load(receipt_path, expected_token=token)
                for receipt_path, token in expected_receipts[:attempts]
            ]
            if receipts[-1].status != "ready_for_verification":
                raise RecordError("accepted verdict requires a ready_for_verification final receipt")

        return cls(
            schema_version=version,
            run_id=run_id,
            status=status,
            agent=agent,
            attempts=attempts,
            artifacts=tuple(artifacts),
            checks=checks,
            summary=_string(record, "summary"),
            remaining_issues=issues,
        )

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status,
            "agent": self.agent.value,
            "attempts": self.attempts,
            "artifacts": list(self.artifacts),
            "checks": [check.to_json() for check in self.checks],
            "summary": self.summary,
            "remaining_issues": list(self.remaining_issues),
        }


@dataclass(frozen=True, slots=True)
class WorkerReceipt:
    token: str
    status: str
    summary: str

    @classmethod
    def load(cls, path: Path, *, expected_token: str) -> "WorkerReceipt":
        # token 将回执绑定到唯一 attempt，避免旧文件或其他会话被误认为本轮完成。
        record = read_json_object(path)
        if _integer(record, "schema_version", minimum=1) != 1:
            raise RecordError("unsupported worker receipt schema")
        token = _string(record, "token")
        if token != expected_token:
            raise RecordError("worker receipt token does not match this attempt")
        status = _string(record, "status")
        if status not in {"ready_for_verification", "blocked"}:
            raise RecordError("worker receipt has an unsupported status")
        return cls(token=token, status=status, summary=_string(record, "summary"))

    def to_json(self) -> JsonObject:
        return {
            "schema_version": 1,
            "token": self.token,
            "status": self.status,
            "summary": self.summary,
        }
