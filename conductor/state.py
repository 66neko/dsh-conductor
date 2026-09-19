"""存放在委派工作区中的单次运行状态。"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import AgentKind, JsonObject, worker_result_path


def default_state_root(workspace: Path) -> Path:
    """返回工作区内默认的运行记录根目录。"""
    return workspace.expanduser().resolve() / ".dsh-conductor"


def atomic_write_json(path: Path, value: object) -> None:
    # 先 fsync 临时文件再原子替换，读取方不会观察到半写入的 JSON。
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@dataclass(frozen=True, slots=True)
class AttemptState:
    number: int
    token: str
    task_file: Path
    receipt_file: Path

    @property
    def result_file(self) -> Path:
        return worker_result_path(self.receipt_file)

    def to_json(self) -> JsonObject:
        return {
            "number": self.number,
            "token": self.token,
            "task_file": str(self.task_file),
            "result_file": str(self.result_file),
            "receipt_file": str(self.receipt_file),
        }


@dataclass(frozen=True, slots=True)
class AgentState:
    kind: AgentKind
    session: str
    attempts: tuple[AttemptState, ...]

    def to_json(self) -> JsonObject:
        return {
            "agent": self.kind.value,
            "session": self.session,
            "attempts": [attempt.to_json() for attempt in self.attempts],
        }


@dataclass(frozen=True, slots=True)
class RunState:
    run_id: str
    root: Path
    workspace: Path
    request_file: Path
    user_prompt_file: Path
    manager_prompt_file: Path
    plan_file: Path
    verdict_file: Path
    worker_log_file: Path
    agents: tuple[AgentState, ...]

    @classmethod
    def create(
        cls,
        *,
        state_root: Path,
        workspace: Path,
        prompt: str,
        available_agents: set[AgentKind],
        max_attempts: int,
        worker_idle_timeout_seconds: int,
        keep_session: bool,
        max_recovery_attempts: int = 5,
        sdk_heartbeat_counts_as_activity: bool = True,
    ) -> "RunState":
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        random_id = secrets.token_hex(6)
        run_id = f"{timestamp}-{random_id}"
        # 每次运行创建不可复用的新目录，从路径层面隔离旧 plan、verdict 与 receipt。
        root = state_root.expanduser().resolve() / "runs" / run_id
        root.mkdir(parents=True, exist_ok=False)

        agent_states: list[AgentState] = []
        for kind in AgentKind:
            attempts: list[AttemptState] = []
            for number in range(1, max_attempts + 1):
                attempt_root = root / "attempts" / kind.value / str(number)
                attempt_root.mkdir(parents=True)
                attempts.append(
                    AttemptState(
                        number=number,
                        token=secrets.token_hex(24),
                        task_file=attempt_root / "task.md",
                        receipt_file=attempt_root / "receipt.json",
                    )
                )
            agent_states.append(
                AgentState(
                    kind=kind,
                    session=f"dsh-{kind.value}-{random_id}",
                    attempts=tuple(attempts),
                )
            )

        user_prompt_file = root / "user-prompt.md"
        user_prompt_file.write_text(prompt.rstrip() + "\n", encoding="utf-8")
        request_file = root / "request.json"
        manager_prompt_file = root / "manager-prompt.md"
        plan_file = root / "plan.json"
        verdict_file = root / "verdict.json"
        worker_log_file = root / "worker-screen.log"
        request: JsonObject = {
            "schema_version": 2,
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "workspace": str(workspace.resolve()),
            "user_prompt_file": str(user_prompt_file),
            "plan_file": str(plan_file),
            "verdict_file": str(verdict_file),
            "worker_log_file": str(worker_log_file),
            "max_attempts": max_attempts,
            "worker_idle_timeout_seconds": worker_idle_timeout_seconds,
            "max_recovery_attempts": max_recovery_attempts,
            "sdk_heartbeat_counts_as_activity": sdk_heartbeat_counts_as_activity,
            "supervision_file": str(root / "supervision.json"),
            "supervision_log_file": str(root / "supervision.jsonl"),
            "keep_session": keep_session,
            "available_agents": [kind.value for kind in AgentKind if kind in available_agents],
            "agents": [item.to_json() for item in agent_states],
        }
        atomic_write_json(request_file, request)
        state = cls(
            run_id=run_id,
            root=root,
            workspace=workspace.resolve(),
            request_file=request_file,
            user_prompt_file=user_prompt_file,
            manager_prompt_file=manager_prompt_file,
            plan_file=plan_file,
            verdict_file=verdict_file,
            worker_log_file=worker_log_file,
            agents=tuple(agent_states),
        )
        atomic_write_json(
            state_root.expanduser().resolve() / "latest.json",
            {"schema_version": 1, "run_id": run_id, "run_directory": str(root)},
        )
        return state

    def agent_state(self, kind: AgentKind) -> AgentState:
        for agent in self.agents:
            if agent.kind is kind:
                return agent
        raise ValueError(f"run state has no definition for agent {kind.value}")

    def write_manager_prompt(self, prompt: str) -> None:
        self.manager_prompt_file.write_text(prompt.rstrip() + "\n", encoding="utf-8")
