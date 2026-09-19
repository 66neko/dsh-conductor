"""面向 Python 调用方的高层 conductor SDK。"""

from __future__ import annotations

import fcntl
import math
from dataclasses import dataclass, field
from pathlib import Path

from .dsh import DshClient, DshConfig, DshError, RunResult
from .models import ExecutionPlan, JsonObject, RecordError, Verdict, read_json_object
from .progress import EventCallback, ProgressReporter
from .prompt import build_prompt
from .skills import (
    available_workspace_agent_skills,
    dsh_home,
    prepare_workspace_skills,
)
from .state import RunState, atomic_write_json, default_state_root
from .tmux import HISTORY_LIMIT, TmuxError, TmuxSession
from .worker_log import DEFAULT_WORKER_LOG_INTERVAL_SECONDS, WorkerLogFollower


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
    state_dir: Path | None = None
    provider: str = "deepseek-official"
    model: str = "deepseek-flash"
    max_attempts: int = 2
    worker_idle_timeout_seconds: int = 300
    max_recovery_attempts: int = 5
    sdk_heartbeat_counts_as_activity: bool = True
    timeout_seconds: float = 3600.0
    keep_session: bool = False
    heartbeat_seconds: float = 10.0
    worker_log: bool = True
    worker_log_interval_seconds: float = DEFAULT_WORKER_LOG_INTERVAL_SECONDS
    dsh_init_timeout_seconds: float = 30.0
    dsh_shutdown_timeout_seconds: float = 5.0
    dsh_extra_env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        numeric = {
            "max_attempts": self.max_attempts,
            "max_recovery_attempts": self.max_recovery_attempts,
            "worker_idle_timeout_seconds": self.worker_idle_timeout_seconds,
            "timeout_seconds": self.timeout_seconds,
            "heartbeat_seconds": self.heartbeat_seconds,
            "worker_log_interval_seconds": self.worker_log_interval_seconds,
            "dsh_init_timeout_seconds": self.dsh_init_timeout_seconds,
            "dsh_shutdown_timeout_seconds": self.dsh_shutdown_timeout_seconds,
        }
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("max_attempts must be an integer")
        if not isinstance(self.sdk_heartbeat_counts_as_activity, bool):
            raise ValueError("sdk_heartbeat_counts_as_activity must be a boolean")
        if isinstance(self.worker_idle_timeout_seconds, bool) or not isinstance(self.worker_idle_timeout_seconds, int):
            raise ValueError("worker_idle_timeout_seconds must be an integer")
        if isinstance(self.max_recovery_attempts, bool) or not isinstance(self.max_recovery_attempts, int) or not 1 <= self.max_recovery_attempts <= 5:
            raise ValueError("max_recovery_attempts must be an integer from 1 to 5")
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
    worker_result: Path | None = None

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
        if self.worker_result is not None:
            value["worker_result"] = str(self.worker_result)
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
        state_root = config.state_dir or default_state_root(self.workspace)
        try:
            # 直接覆盖项目级目录，确保 DSH 使用当前 SDK 随包的两个 skill。
            skill_root = prepare_workspace_skills(self.workspace)
            skill_scripts, available_agents = available_workspace_agent_skills(self.workspace)
            state = RunState.create(
                state_root=state_root,
                workspace=self.workspace,
                prompt=prompt,
                available_agents=available_agents,
                max_attempts=config.max_attempts,
                worker_idle_timeout_seconds=config.worker_idle_timeout_seconds,
                max_recovery_attempts=config.max_recovery_attempts,
                sdk_heartbeat_counts_as_activity=config.sdk_heartbeat_counts_as_activity,
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
                worker_idle_timeout_seconds=config.worker_idle_timeout_seconds,
                keep_session=config.keep_session,
                sdk_heartbeat_counts_as_activity=config.sdk_heartbeat_counts_as_activity,
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
            heartbeat_file=state.root / "sdk-heartbeat.json" if config.sdk_heartbeat_counts_as_activity else None,
            supervision_log=state.root / "supervision.jsonl",
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

        finalized = False
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

            # DSH 必须保留会话到 SDK 完成末次采样，避免 10 秒采样间隔吞掉结束前的输出。
            # 失败必须停止 worker；日志和证据保留在运行目录。
            if verdict.status == "rejected":
                self._stop_workers(state, reporter, reason=verdict.summary)
            elif not config.keep_session:
                try:
                    session = TmuxSession(selected.session)
                    if session.exists():
                        status = session.status()
                        if status.agent != plan.agent.value or status.workspace != str(self.workspace):
                            raise TmuxError("worker session metadata does not match this run")
                        session.close()
                except TmuxError as exc:
                    reporter.emit(source="conductor", kind="cleanup_error", message=f"tmux cleanup: {exc}")

            reporter.read_supervision()
            reporter.emit(source="conductor", kind="run_end", message=f"verdict {verdict.status}")
            finalized = True
            worker_result = (
                selected.attempts[verdict.attempts - 1].result_file
                if verdict.attempts else None
            )
            return TaskResult(
                run_id=state.run_id,
                workspace=self.workspace,
                state_directory=state.root,
                session=selected.session,
                plan=plan,
                verdict=verdict,
                dsh=DshRunSummary.from_run(run),
                worker_log=state.worker_log_file if state.worker_log_file.exists() else None,
                worker_result=worker_result if worker_result and worker_result.is_file() else None,
            )
        finally:
            if not finalized:
                self._stop_workers(state, reporter, reason="SDK/DSH 异常、超时或结果无效，结束 worker")
            reporter.stop()

    def _stop_workers(self, state: RunState, reporter: ProgressReporter, *, reason: str) -> None:
        # 与控制器操作互斥，结束标记阻止尚未退出的 watch/recover 再次提交。
        try:
            with (state.root / "supervision.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                atomic_write_json(state.root / "sdk-stop.json", {"run_id": state.run_id, "reason": reason})
                for agent in state.agents:
                    try:
                        session = TmuxSession(agent.session)
                        if not session.exists():
                            continue
                        status = session.status()
                        if status.agent != agent.kind.value or Path(status.workspace).resolve() != self.workspace:
                            raise TmuxError("worker session metadata does not match this run")
                        try:
                            screen = session.capture(history_lines=HISTORY_LIMIT)
                            (state.root / f"sdk-stop-{agent.kind.value}.txt").write_text(screen, encoding="utf-8")
                        finally:
                            session.close()
                        reporter.emit(source="conductor", kind="supervision_stopped", message=f"{agent.kind.value}: {reason}")
                    except (TmuxError, OSError) as exc:
                        reporter.emit(source="conductor", kind="cleanup_error", message=f"tmux stop: {exc}")
                path = state.root / "supervision.json"
                if path.exists():
                    supervision = read_json_object(path)
                    if supervision.get("run_id") == state.run_id:
                        supervision.update(phase="stopped", stop_reason=reason)
                        atomic_write_json(path, supervision)
        except (OSError, RecordError) as exc:
            reporter.emit(source="conductor", kind="cleanup_error", message=f"worker stop: {exc}")
