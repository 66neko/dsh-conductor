"""面向 Python 调用方的高层 conductor SDK。"""

from __future__ import annotations

import shlex
import shutil
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import ConductorError, OperationError
from .lifecycle import RunContext, acquire_lock, positive_seconds
from .runtime import CleanupReport, RunResources, cleanup_resources
from .dsh import DshClient, DshConfig, RunResult
from .models import ExecutionPlan, JsonObject, RecordError, Verdict
from .progress import EventCallback, ProgressReporter
from .reports import build_report
from .prompt import build_prompt
from .skills import (
    available_workspace_agent_skills,
    dsh_home,
    prepare_workspace_skills,
)
from .state import RunState, atomic_write_json, default_state_root
from .tmux import HISTORY_LIMIT, TmuxSession
from .worker_log import DEFAULT_WORKER_LOG_INTERVAL_SECONDS, WorkerLogFollower


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
    cleanup_timeout_seconds: float = 5.0
    keep_session: bool = False
    heartbeat_seconds: float = 10.0
    worker_log: bool = True
    worker_log_interval_seconds: float = DEFAULT_WORKER_LOG_INTERVAL_SECONDS
    dsh_init_timeout_seconds: float = 30.0
    dsh_shutdown_timeout_seconds: float = 5.0
    dsh_extra_env: dict[str, str] = field(default_factory=dict)
    include_report: bool = False

    def __post_init__(self) -> None:
        numeric = {
            "max_attempts": self.max_attempts,
            "max_recovery_attempts": self.max_recovery_attempts,
            "worker_idle_timeout_seconds": self.worker_idle_timeout_seconds,
            "timeout_seconds": self.timeout_seconds,
            "cleanup_timeout_seconds": self.cleanup_timeout_seconds,
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
            positive_seconds(value, name)
        for name in ("keep_session", "worker_log", "include_report"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        for name in ("provider", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.dsh_extra_env, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                            for k, v in self.dsh_extra_env.items()):
            raise ValueError("dsh_extra_env must map strings to strings")
        if any(not k or "=" in k or "\0" in k or "\0" in v for k, v in self.dsh_extra_env.items()):
            raise ValueError("dsh_extra_env contains an invalid environment entry")
        for name in ("state_dir", "dsh_home"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, (str, Path)):
                    raise ValueError(f"{name} must be a path")
                object.__setattr__(self, name, Path(value))
        object.__setattr__(self, "dsh_extra_env", dict(self.dsh_extra_env))


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
    cleanup: CleanupReport = field(default_factory=CleanupReport)
    tmux_socket: Path | None = None
    report: str | None = None
    report_file: Path | None = None
    report_warnings: tuple[str, ...] = ()

    @property
    def attach_command(self) -> str:
        return shlex.join(["tmux", *(["-S", str(self.tmux_socket)] if self.tmux_socket else []),
                           "attach", "-t", self.session])

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
            "cleanup": self.cleanup.to_json(),
            "attach_command": self.attach_command,
        }
        if self.tmux_socket is not None:
            value["tmux_socket"] = str(self.tmux_socket)
        if self.worker_log is not None:
            value["worker_log"] = str(self.worker_log)
        if self.worker_result is not None:
            value["worker_result"] = str(self.worker_result)
        if self.report is not None:
            value["report"] = self.report
            if self.report_file is not None:
                value["report_file"] = str(self.report_file)
            if self.report_warnings:
                value["report_warnings"] = list(self.report_warnings)
        return value


class Conductor:
    """让 DSH 拆解、监督并验收一个自然语言编码任务。"""

    def __init__(self, workspace: str | Path, config: ConductorConfig | None = None) -> None:
        try:
            self.workspace = Path(workspace).expanduser().resolve()
        except (TypeError, ValueError, OSError) as exc:
            raise ConductorError(str(exc), code="invalid_input", phase="validation") from exc
        self.config = config or ConductorConfig()

    def run(self, prompt: str, on_event: EventCallback | None = None, *,
            cancel_event: threading.Event | None = None) -> TaskResult:
        config = self.config
        context = RunContext(config.timeout_seconds, config.cleanup_timeout_seconds, cancel_event)
        state: RunState | None = None
        resources: RunResources | None = None
        client: DshClient | None = None
        reporter: ProgressReporter | None = None
        followers: list[WorkerLogFollower] = []
        workspace_lock = None
        result: TaskResult | None = None
        validation_object = "plan"
        cleanup = CleanupReport()
        diagnostics: list[str] = []
        try:
            if cancel_event is not None and not isinstance(cancel_event, threading.Event):
                raise ConductorError("cancel_event must be a threading.Event", code="invalid_input", phase="validation")
            context.budget().check()
            if not self.workspace.is_dir():
                raise ConductorError(f"workspace is not a directory: {self.workspace}", code="invalid_input", phase="validation")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ConductorError("prompt must not be empty", code="invalid_input", phase="validation")
            if on_event is not None and not callable(on_event):
                raise ConductorError("on_event must be callable", code="invalid_input", phase="validation")
            budget = context.budget("preparation")
            if shutil.which("tmux") is None:
                raise OperationError("cannot find tmux on PATH", code="dependency_missing", phase="preparation")
            # 与自定义 state_dir 无关，同一工作区始终共用一把锁。
            lock_root = default_state_root(self.workspace)
            lock_root.mkdir(parents=True, exist_ok=True)
            workspace_lock = (lock_root / "workspace.lock").open("a")
            acquire_lock(workspace_lock, budget, immediate=True)
            skill_root = prepare_workspace_skills(self.workspace, budget=budget)
            skill_scripts, available_agents = available_workspace_agent_skills(self.workspace)

            def created(run_id: str, root: Path) -> None:
                context.run_id = run_id
                context.state_directory = root

            state = RunState.create(
                state_root=config.state_dir or default_state_root(self.workspace), workspace=self.workspace,
                prompt=prompt, available_agents=available_agents, max_attempts=config.max_attempts,
                worker_idle_timeout_seconds=config.worker_idle_timeout_seconds,
                max_recovery_attempts=config.max_recovery_attempts,
                sdk_heartbeat_counts_as_activity=config.sdk_heartbeat_counts_as_activity,
                keep_session=config.keep_session, budget=budget, on_create=created,
                include_report=config.include_report,
            )
            budget = context.budget()
            resources = RunResources(state, context)
            manager_prompt = build_prompt(state, skill_scripts=skill_scripts, available_agents=available_agents,
                                          max_attempts=config.max_attempts,
                                          worker_idle_timeout_seconds=config.worker_idle_timeout_seconds,
                                          keep_session=config.keep_session,
                                          sdk_heartbeat_counts_as_activity=config.sdk_heartbeat_counts_as_activity,
                                          include_report=config.include_report)
            state.write_manager_prompt(manager_prompt)
            budget.check()
            reporter = ProgressReporter(on_event=on_event, heartbeat_seconds=config.heartbeat_seconds,
                                        heartbeat_file=state.root / "sdk-heartbeat.json" if config.sdk_heartbeat_counts_as_activity else None,
                                        supervision_log=state.root / "supervision.jsonl")
            reporter.start()
            reporter.emit(source="conductor", kind="run_start", message=f"run {state.run_id}")
            reporter.emit(source="conductor", kind="state", message=f"state {state.root}")
            if config.worker_log:
                for agent in state.agents:
                    follower = WorkerLogFollower(session_name=agent.session, agent=agent.kind.value,
                                                 log_file=state.worker_log_file, sink=reporter.worker_lines,
                                                 interval_seconds=config.worker_log_interval_seconds,
                                                 socket_path=resources.socket, budget=budget)
                    followers.append(follower)
                    follower.start()
            client = DshClient(DshConfig(
                workspace=self.workspace, dsh_bin=config.dsh_bin, provider=config.provider, model=config.model,
                dsh_home=dsh_home(config.dsh_home), skill_dir=skill_root,
                init_timeout_seconds=config.dsh_init_timeout_seconds,
                shutdown_timeout_seconds=config.dsh_shutdown_timeout_seconds,
                extra_env=dict(config.dsh_extra_env), budget=context.budget("dsh_start"),
            ))
            run = client.run(manager_prompt, session_id=f"conductor-{state.run_id}",
                             timeout_seconds=config.timeout_seconds, on_event=reporter)
            if run.status != "completed":
                raise OperationError(f"DSH turn ended with status {run.status}: {run.stderr_tail[-1200:]}",
                                     code="timeout" if run.status == "timeout" else "dsh_execution_failed",
                                     phase="dsh_run", details={"turn_end_reason": run.turn_end_reason})
            budget = context.budget("result_validation")
            budget.check()
            plan = ExecutionPlan.load(state.plan_file, expected_run_id=state.run_id, budget=budget)
            selected = state.agent_state(plan.agent)
            validation_object = "verdict"
            verdict = Verdict.load(state.verdict_file, plan=plan, max_attempts=config.max_attempts,
                                   workspace=self.workspace, budget=budget,
                                   expected_receipts=tuple((attempt.receipt_file, attempt.token) for attempt in selected.attempts))
            if verdict.status == "accepted" and plan.agent not in available_agents:
                raise RecordError(f"accepted verdict selected unavailable agent {plan.agent.value!r}")
            worker_result = selected.attempts[verdict.attempts - 1].result_file if verdict.attempts else None
            budget.check()
            result = TaskResult(run_id=state.run_id, workspace=self.workspace, state_directory=state.root,
                                session=selected.session, plan=plan, verdict=verdict, dsh=DshRunSummary.from_run(run),
                                worker_result=worker_result if worker_result and worker_result.is_file() else None,
                                tmux_socket=resources.socket)
            if config.include_report:
                report = build_report(state, plan=plan, verdict=verdict, budget=budget)
                result = replace(result, report=report.text, report_file=report.path, report_warnings=report.warnings)
        except BaseException as exc:
            context.first_error = exc
        finally:
            cleanup_started = time.monotonic()
            cleanup_budget = context.cleanup_budget()

            def collect(action, label: str) -> bool:
                try:
                    action()
                    return True
                except BaseException as exc:
                    if not isinstance(exc, Exception) and context.first_error is None:
                        context.first_error = exc
                    diagnostics.append(f"{label}: {exc}")
                    return False

            # 停止标记不等待 supervision.lock。控制器的锁和所有等待也检查该标记。
            if state is not None:
                collect(lambda: atomic_write_json(state.root / "sdk-stop.json",
                        {"run_id": state.run_id, "reason": str(context.first_error) if context.first_error else "run finalized"}), "stop marker")
            if client is not None:
                collect(lambda: client.close(budget=cleanup_budget.limit(max(0, cleanup_budget.deadline - time.monotonic()) * 0.4),
                                             graceful=context.first_error is None), "DSH cleanup")
                diagnostics.extend(client.close_errors)
            for follower in followers:
                collect(lambda f=follower: f.stop(budget=cleanup_budget.limit(
                        min(0.5, max(0, cleanup_budget.deadline - time.monotonic()) * 0.15))), "worker log shutdown")
            if resources is not None and state is not None:
                # worker_log=False 时异常也尽力留下末屏，采样不能挤占回收所需预算。
                if context.first_error is not None or (result is not None and not result.accepted):
                    evidence_budget = cleanup_budget.limit(min(0.5, max(0, cleanup_budget.deadline - time.monotonic()) * 0.15))
                    for agent in state.agents:
                        def capture(agent=agent) -> None:
                            session = TmuxSession(agent.session, socket_path=resources.socket, budget=evidence_budget)
                            if session.exists():
                                status = session.status()
                                if status.run_id != state.run_id or status.workspace != str(self.workspace):
                                    raise ValueError("worker identity does not match this run")
                                (state.root / f"sdk-stop-{agent.kind.value}.txt").write_text(
                                    session.capture(history_lines=HISTORY_LIMIT), encoding="utf-8")
                        collect(capture, "final evidence")
                retain = result.session if result is not None and result.accepted and config.keep_session and context.first_error is None else None
                try:
                    cleanup = cleanup_resources(resources.data, cleanup_budget, retain_session=retain)
                except BaseException as exc:
                    if not isinstance(exc, Exception) and context.first_error is None:
                        context.first_error = exc
                    cleanup = CleanupReport("incomplete", remaining_resources=({"kind": "run", "path": str(state.root)},), errors=(str(exc),))
            if reporter is not None:
                collect(lambda: reporter.emit(source="conductor", kind="run_end",
                        message=f"verdict {result.verdict.status}" if result is not None and context.first_error is None else "run failed"), "final event")
                collect(lambda: reporter.stop(budget=cleanup_budget), "reporter shutdown")
                if reporter.dropped_events:
                    diagnostics.append(f"{reporter.dropped_events} progress events were dropped")
                if reporter.callback_inflight:
                    diagnostics.append("a caller callback is still running; further delivery was stopped")
            thread_resources = tuple({"kind": "thread", "name": f"worker-log-{f.agent}"}
                                     for f in followers if f.is_alive)
            if reporter is not None and reporter.heartbeat_alive:
                thread_resources += ({"kind": "thread", "name": "dsh-heartbeat"},)
            for reaper in context.reapers:
                reaper.join(timeout=max(0, min(0.05, cleanup_budget.deadline - time.monotonic())))
            thread_resources += tuple({"kind": "thread", "name": reaper.name}
                                      for reaper in context.reapers if reaper.is_alive()
                                      and not any(item.get("name") == reaper.name for item in cleanup.remaining_resources))
            cleanup = replace(cleanup, elapsed_seconds=time.monotonic() - cleanup_started,
                              errors=cleanup.errors + tuple(diagnostics),
                              timed_out=cleanup.timed_out or bool(thread_resources and time.monotonic() >= cleanup_budget.deadline),
                              status="incomplete" if thread_resources else cleanup.status,
                              remaining_resources=cleanup.remaining_resources + thread_resources)
            if resources is not None:
                if not collect(lambda: resources.finish(cleanup), "persist cleanup report"):
                    cleanup = replace(cleanup, errors=cleanup.errors + (diagnostics[-1],))
                if not collect(resources.release, "release run lock"):
                    cleanup = replace(cleanup, status="incomplete", errors=cleanup.errors + (diagnostics[-1],),
                                      remaining_resources=cleanup.remaining_resources + ({"kind": "run_lock"},))
            if workspace_lock is not None:
                if not collect(workspace_lock.close, "release workspace lock"):
                    cleanup = replace(cleanup, status="incomplete", errors=cleanup.errors + (diagnostics[-1],),
                                      remaining_resources=cleanup.remaining_resources + ({"kind": "workspace_lock"},))
            if client is not None:
                collect(client.reap, "reap DSH")
        if result is not None:
            result = replace(result, cleanup=cleanup, worker_log=state.worker_log_file if state and state.worker_log_file.exists() else None)
        error = context.first_error
        if error is None and cleanup.status == "incomplete":
            error = ConductorError("required run resources could not be cleaned up", code="cleanup_failed", phase="cleanup")
        if error is not None:
            if not isinstance(error, Exception):
                raise error
            if isinstance(error, ConductorError):
                failure = error
            elif isinstance(error, OperationError):
                failure = ConductorError(str(error), code=error.code, phase=error.phase, details=error.details)
            elif isinstance(error, RecordError):
                failure = ConductorError(f"invalid or missing DSH result: {error}" if context.phase == "result_validation" else str(error),
                                         code="result_invalid" if context.phase == "result_validation" else "preparation_failed",
                                         phase=context.phase,
                                         details={"validation_object": validation_object} if context.phase == "result_validation" else {})
            elif isinstance(error, OSError) and context.phase == "preparation":
                failure = ConductorError(str(error), code="preparation_failed", phase=context.phase)
            else:
                failure = ConductorError(str(error), code="internal_error", phase=context.phase)
            failure.run_id = context.run_id
            failure.state_directory = context.state_directory
            failure.cleanup = cleanup
            failure.result = result
            if config.include_report and state is not None:
                if result is not None and result.report is not None:
                    failure.report, failure.report_file = result.report, result.report_file
                    failure.report_warnings = result.report_warnings
                else:
                    # 核心清理已完成；仅使用剩余清理预算，绝不覆盖首次执行错误。
                    report = build_report(state, plan=result.plan if result else None,
                                          verdict=result.verdict if result else None,
                                          budget=cleanup_budget, failure=str(failure), partial=True)
                    failure.report, failure.report_file = report.text, report.path
                    failure.report_warnings = report.warnings
            if failure is error:
                raise failure
            raise failure from error
        assert result is not None
        return result
