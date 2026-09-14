"""存放在委派工作区之外的单次运行状态。"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import AgentKind, JsonObject


def default_state_root() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    return base / "dsh-conductor"


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

    def to_json(self) -> JsonObject:
        return {
            "number": self.number,
            "token": self.token,
            "task_file": str(self.task_file),
            "receipt_file": str(self.receipt_file),
        }


@dataclass(frozen=True, slots=True)
class RunState:
    run_id: str
    root: Path
    workspace: Path
    agent: AgentKind
    session: str
    request_file: Path
    prompt_file: Path
    verdict_file: Path
    attempts: tuple[AttemptState, ...]

    @classmethod
    def create(
        cls,
        *,
        state_root: Path,
        workspace: Path,
        agent: AgentKind,
        task: str,
        acceptance: str,
        max_attempts: int,
        attempt_timeout_seconds: int,
        keep_session: bool,
    ) -> "RunState":
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        random_id = secrets.token_hex(6)
        run_id = f"{timestamp}-{random_id}"
        # 每次运行创建不可复用的新目录，从路径层面隔离旧 verdict 与旧 receipt。
        root = state_root.expanduser().resolve() / "runs" / run_id
        root.mkdir(parents=True, exist_ok=False)
        session = f"dsh-{agent.value}-{random_id}"

        attempts: list[AttemptState] = []
        for number in range(1, max_attempts + 1):
            attempt_root = root / "attempts" / str(number)
            attempt_root.mkdir(parents=True)
            attempts.append(
                AttemptState(
                    number=number,
                    token=secrets.token_hex(24),
                    task_file=attempt_root / "task.md",
                    receipt_file=attempt_root / "receipt.json",
                )
            )
        attempts[0].task_file.write_text(task.rstrip() + "\n", encoding="utf-8")
        acceptance_file = root / "acceptance.md"
        acceptance_file.write_text(acceptance.rstrip() + "\n", encoding="utf-8")

        request_file = root / "request.json"
        prompt_file = root / "orchestrator-prompt.md"
        verdict_file = root / "verdict.json"
        request: JsonObject = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "workspace": str(workspace.resolve()),
            "agent": agent.value,
            "session": session,
            "task_file": str(attempts[0].task_file),
            "acceptance_file": str(acceptance_file),
            "verdict_file": str(verdict_file),
            "max_attempts": max_attempts,
            "attempt_timeout_seconds": attempt_timeout_seconds,
            "keep_session": keep_session,
            "attempts": [attempt.to_json() for attempt in attempts],
        }
        atomic_write_json(request_file, request)
        state = cls(
            run_id=run_id,
            root=root,
            workspace=workspace.resolve(),
            agent=agent,
            session=session,
            request_file=request_file,
            prompt_file=prompt_file,
            verdict_file=verdict_file,
            attempts=tuple(attempts),
        )
        atomic_write_json(
            state_root.expanduser().resolve() / "latest.json",
            {"schema_version": 1, "run_id": run_id, "run_directory": str(root)},
        )
        return state

    def write_prompt(self, prompt: str) -> None:
        self.prompt_file.write_text(prompt.rstrip() + "\n", encoding="utf-8")

    def error_output(self, message: str, *, dsh_status: str | None = None) -> JsonObject:
        output: JsonObject = {
            "schema_version": 1,
            "run_id": self.run_id,
            "status": "error",
            "agent": self.agent.value,
            "workspace": str(self.workspace),
            "state_directory": str(self.root),
            "error": message,
        }
        if dsh_status is not None:
            output["dsh_status"] = dsh_status
        return output
