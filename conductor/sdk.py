"""面向 Python 调用方的高层 conductor SDK。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from .dsh import DshClient, DshConfig, DshError, RunResult
from .models import ExecutionPlan, JsonObject, RecordError, Verdict
from .progress import EventCallback, ProgressReporter
from .prompt import build_prompt
from .skills import (
    available_workspace_agent_skills,
    dsh_home,
    prepare_workspace_skills,
)
from .state import RunState, default_state_root
from .worker_log import WorkerLogFollower


class ConductorError(RuntimeError):
    """任务未能产生可信的结构化结果。"""

    def __init__(
        self,
        message: str,
        *,
        run_id: str | None = None,
        state_directory: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.state_directory = state_directory

    def to_json(self) -> JsonObject:
        value: JsonObject = {
            "schema_version": 1,
            "status": "error",
            "error": str(self),
        }
        if self.run_id is not None:
            value["run_id"] = self.run_id
        if self.state_directory is not None:
            value["state_directory"] = str(self.state_directory)
        return value


@dataclass(frozen=True, slots=True)
class ConductorConfig:
    """一次或多次 SDK 调用共用的运行配置。"""

    dsh_bin: str | None = None
    dsh_home: Path | None = None
    state_dir: Path = field(default_factory=default_state_root)
    provider: str = "deepseek-official"
    model: str = "deepseek-flash"
    max_attempts: int = 2
    attempt_timeout_seconds: int = 1200
    timeout_seconds: float = 3600.0
    keep_session: bool = False
    heartbeat_seconds: float = 10.0
    worker_log: bool = True
    worker_log_interval_seconds: float = 5.0
    dsh_init_timeout_seconds: float = 30.0
    dsh_shutdown_timeout_seconds: float = 5.0
    dsh_extra_env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        numeric = {
            "max_attempts": self.max_attempts,
            "attempt_timeout_seconds": self.attempt_timeout_seconds,
            "timeout_seconds": self.timeout_seconds,
            "heartbeat_seconds": self.heartbeat_seconds,
            "worker_log_interval_seconds": self.worker_log_interval_seconds,
            "dsh_init_timeout_seconds": self.dsh_init_timeout_seconds,
            "dsh_shutdown_timeout_seconds": self.dsh_shutdown_timeout_seconds,
        }
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("max_attempts must be an integer")
        if isinstance(self.attempt_timeout_seconds, bool) or not isinstance(self.attempt_timeout_seconds, int):
            raise ValueError("attempt_timeout_seconds must be an integer")
        for name, value in numeric.items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be greater than zero")


@dataclass(frozen=True, slots=True)
class DshRunSummary:
    status: str
    elapsed_seconds: float
    event_count: int
    final_text: str

    @classmethod
    def from_run(cls, run: RunResult) -> "DshRunSummary":
        return cls(
            status=run.status,
            elapsed_seconds=run.elapsed_seconds,
            event_count=len(run.events),
            final_text=run.final_text,
        )

    def to_json(self) -> JsonObject:
        return {
            "status": self.status,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "event_count": self.event_count,
            "final_text": self.final_text,
        }


@dataclass(frozen=True, slots=True)
class TaskResult:
    """DSH 计划、独立验收结论和本轮审计信息。"""

    run_id: str
    workspace: Path
    state_directory: Path
    session: str
    plan: ExecutionPlan
    verdict: Verdict
    dsh: DshRunSummary
    worker_log: Path | None = None

    @property
    def accepted(self) -> bool:
        return self.verdict.status == "accepted"

    def to_json(self) -> JsonObject:
        value: JsonObject = {
            "schema_version": 1,
            "run_id": self.run_id,
            "status": self.verdict.status,
            "workspace": str(self.workspace),
            "state_directory": str(self.state_directory),
            "session": self.session,
            "plan": self.plan.to_json(),
            "verdict": self.verdict.to_json(),
            "dsh": self.dsh.to_json(),
        }
        if self.worker_log is not None:
            value["worker_log"] = str(self.worker_log)
        return value


class Conductor:
    """让 DSH 拆解、监督并验收一个自然语言编码任务。"""

    def __init__(self, workspace: str | Path, config: ConductorConfig | None = None) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.config = config or ConductorConfig()

    def run(self, prompt: str, on_event: EventCallback | None = None) -> TaskResult:
        if not self.workspace.is_dir():
            raise ConductorError(f"workspace is not a directory: {self.workspace}")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ConductorError("prompt must not be empty")

        config = self.config
        home = dsh_home(config.dsh_home)
        try:
            # 直接覆盖项目级目录，确保 DSH 使用当前 SDK 随包的两个 skill。
            skill_root = prepare_workspace_skills(self.workspace)
            skill_scripts, available_agents = available_workspace_agent_skills(self.workspace)
            state = RunState.create(
                state_root=config.state_dir,
                workspace=self.workspace,
                prompt=prompt,
                available_agents=available_agents,
                max_attempts=config.max_attempts,
                attempt_timeout_seconds=config.attempt_timeout_seconds,
                keep_session=config.keep_session,
            )
        except (DshError, OSError) as exc:
            raise ConductorError(str(exc)) from exc

        try:
            manager_prompt = build_prompt(
                state,
                skill_scripts=skill_scripts,
                available_agents=available_agents,
                max_attempts=config.max_attempts,
                attempt_timeout_seconds=config.attempt_timeout_seconds,
                keep_session=config.keep_session,
            )
            state.write_manager_prompt(manager_prompt)
        except OSError as exc:
            raise ConductorError(
                f"cannot prepare manager prompt: {exc}",
                run_id=state.run_id,
                state_directory=state.root,
            ) from exc

        reporter = ProgressReporter(
            on_event=on_event,
            heartbeat_seconds=config.heartbeat_seconds,
        ).start()
        reporter.emit(source="conductor", kind="run_start", message=f"run {state.run_id}")
        reporter.emit(source="conductor", kind="state", message=f"state {state.root}")

        followers: list[WorkerLogFollower] = []
        if config.worker_log:
            # agent 尚未选择，因此同时监听两个唯一候选会话；不存在的会话不会产生事件。
            for agent in state.agents:
                followers.append(
                    WorkerLogFollower(
                        session_name=agent.session,
                        agent=agent.kind.value,
                        log_file=state.worker_log_file,
                        sink=reporter.worker_lines,
                        interval_seconds=config.worker_log_interval_seconds,
                    ).start()
                )

        try:
            run: RunResult | None = None
            try:
                dsh_config = DshConfig(
                    workspace=self.workspace,
                    dsh_bin=config.dsh_bin,
                    provider=config.provider,
                    model=config.model,
                    dsh_home=home,
                    skill_dir=skill_root,
                    init_timeout_seconds=config.dsh_init_timeout_seconds,
                    shutdown_timeout_seconds=config.dsh_shutdown_timeout_seconds,
                    extra_env=dict(config.dsh_extra_env),
                )
                with DshClient(dsh_config) as client:
                    run = client.run(
                        manager_prompt,
                        session_id=f"conductor-{state.run_id}",
                        timeout_seconds=config.timeout_seconds,
                        on_event=reporter,
                    )
            except DshError as exc:
                raise ConductorError(
                    f"DSH failed: {exc}",
                    run_id=state.run_id,
                    state_directory=state.root,
                ) from exc
            finally:
                for follower in followers:
                    follower.stop()

            assert run is not None
            if run.status != "completed":
                message = f"DSH turn ended with status {run.status}"
                if run.stderr_tail:
                    message += f": {run.stderr_tail[-1200:]}"
                raise ConductorError(
                    message,
                    run_id=state.run_id,
                    state_directory=state.root,
                )

            try:
                plan = ExecutionPlan.load(state.plan_file, expected_run_id=state.run_id)
                selected = state.agent_state(plan.agent)
                verdict = Verdict.load(
                    state.verdict_file,
                    plan=plan,
                    max_attempts=config.max_attempts,
                    workspace=self.workspace,
                    expected_receipts=tuple(
                        (attempt.receipt_file, attempt.token) for attempt in selected.attempts
                    ),
                )
                if verdict.status == "accepted" and plan.agent not in available_agents:
                    raise RecordError(
                        f"accepted verdict selected unavailable agent {plan.agent.value!r}"
                    )
            except RecordError as exc:
                raise ConductorError(
                    f"invalid or missing DSH result: {exc}",
                    run_id=state.run_id,
                    state_directory=state.root,
                ) from exc

            reporter.emit(source="conductor", kind="run_end", message=f"verdict {verdict.status}")
            return TaskResult(
                run_id=state.run_id,
                workspace=self.workspace,
                state_directory=state.root,
                session=selected.session,
                plan=plan,
                verdict=verdict,
                dsh=DshRunSummary.from_run(run),
                worker_log=state.worker_log_file if state.worker_log_file.exists() else None,
            )
        finally:
            reporter.stop()
