"""持久化 worker 监督；机械监测与操作由控制器执行，语义判断交给 DSH。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from .models import ExecutionPlan, JsonObject, RecordError, WorkerReceipt, read_json_object
from .state import atomic_write_json
from .lifecycle import acquire_lock
from .runtime import controller_settings
from .tmux import DEFAULT_CAPTURE_HISTORY_LINES, HISTORY_LIMIT, TmuxError, TmuxSession

if TYPE_CHECKING:
    from .agents.base import AgentAdapter


class SupervisionError(RuntimeError):
    """监督请求不符合当前会话/轮次，或恢复额度已耗尽。"""


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Supervisor:
    def __init__(self, request_file: Path, adapter: AgentAdapter, session_name: str) -> None:
        self.request_file = request_file.resolve()
        self.root = self.request_file.parent
        self.request = read_json_object(self.request_file)
        self.adapter = adapter
        self.workspace = Path(self.request["workspace"]).resolve()
        self.socket_path, self.budget, self.runtime_file = controller_settings(self.request, self.root)
        plan = ExecutionPlan.load(Path(self.request["plan_file"]), expected_run_id=self.request["run_id"], budget=self.budget)
        if plan.agent.value != adapter.kind:
            raise SupervisionError("controller agent does not match the execution plan")
        self.agent = next((a for a in self.request["agents"] if a["agent"] == adapter.kind), None)
        if self.agent is None or self.agent["session"] != session_name:
            raise SupervisionError("session does not match this run")
        self.session = TmuxSession(session_name, socket_path=self.socket_path, budget=self.budget,
                                   runtime_file=self.runtime_file)
        self.path = self.root / "supervision.json"
        self.log_file = self.root / "supervision.jsonl"
        self.activity_file = self.root / "worker-activity.json"
        limit = self.request.get("max_recovery_attempts", 5)
        if type(limit) is not int or not 1 <= limit <= 5:
            raise SupervisionError("max_recovery_attempts must be an integer from 1 to 5")

    @contextmanager
    def locked(self) -> Iterator[JsonObject]:
        # 不跨 watch 的等待周期持锁，恢复/结束可以及时取得控制权。
        with (self.root / "supervision.lock").open("a") as handle:
            if self.budget is not None:
                acquire_lock(handle, self.budget)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX)
            state = read_json_object(self.path) if self.path.exists() else {
                "schema_version": 1, "run_id": self.request["run_id"], "agent": self.adapter.kind,
                "session": self.session.name, "phase": "new", "recoveries": 0, "attempt": 0,
                "choices": {}, "observation": 0,
            }
            if (state["run_id"], state["agent"], state["session"]) != (
                self.request["run_id"], self.adapter.kind, self.session.name,
            ):
                raise SupervisionError("supervision state belongs to another run or agent")
            if (self.root / "sdk-stop.json").exists():
                state.update(phase="stopped", stop_reason="SDK 已结束运行")
                self.save(state)
            yield state

    def save(self, state: JsonObject) -> None:
        atomic_write_json(self.path, state)

    def record(self, state: JsonObject, event: str, **details: object) -> None:
        record = {
            "time": time.time(), "run_id": state["run_id"], "agent": state["agent"],
            "session": state["session"], "attempt": state["attempt"], "event": event,
            "recoveries": state["recoveries"], "max_recoveries": self.request.get("max_recovery_attempts", 5), **details,
        }
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def attached(self) -> TmuxSession:
        session = TmuxSession.attach(name=self.session.name, expected_agent=self.adapter.kind,
                                     socket_path=self.socket_path, budget=self.budget, runtime_file=self.runtime_file)
        if Path(session.status().workspace).resolve() != self.workspace:
            raise SupervisionError("worker workspace does not match this run")
        return session

    def attempt(self, state: JsonObject) -> JsonObject:
        if not 1 <= state["attempt"] <= len(self.agent["attempts"]):
            raise SupervisionError("no active attempt")
        return self.agent["attempts"][state["attempt"] - 1]

    def start(self, *, task_file: Path, receipt_file: Path, token: str, submission: str,
              command: list[str] | None = None) -> JsonObject:
        with self.locked() as state:
            if state["phase"] == "stopped":
                raise SupervisionError("run is already stopped")
            number = state["attempt"] + 1
            if number > len(self.agent["attempts"]):
                raise SupervisionError("no remaining business attempts")
            expected = self.agent["attempts"][number - 1]
            if (str(task_file.resolve()), str(receipt_file.resolve()), token) != (
                expected["task_file"], expected["receipt_file"], expected["token"],
            ):
                raise SupervisionError("task/receipt/token do not match the next attempt")
            if not task_file.read_text(encoding="utf-8").strip():
                raise SupervisionError("task file is empty")
            if receipt_file.exists() or Path(expected["result_file"]).exists():
                raise SupervisionError("refusing stale receipt or result")
            if state["attempt"]:
                old = self.attempt(state)
                WorkerReceipt.load(Path(old["receipt_file"]), expected_token=old["token"], budget=self.budget)
                if command is not None:
                    raise SupervisionError("follow-ups must use the original session")
                self.attached()
            else:
                if command is None:
                    raise SupervisionError("first attempt requires run")
                TmuxSession.create(name=self.session.name, workspace=self.workspace, agent=self.adapter.kind,
                                   command=command, activity_file=self.activity_file, socket_path=self.socket_path,
                                   budget=self.budget, runtime_file=self.runtime_file)
            state.update(attempt=number, phase="starting", submission=submission,
                         last_activity_at=time.time(), last_screen=None, last_bytes=0, ready_since=None,
                         acknowledged=None, observation=None)
            self.save(state)
            self.record(state, "submitted", message="任务已登记，watch 将在输入界面就绪后提交")
            return self.response(state, "starting")

    def response(self, state: JsonObject, status: str, **extra: object) -> JsonObject:
        attempt = self.attempt(state) if state["attempt"] else {}
        return {
            "schema_version": 1, "run_id": state["run_id"], "agent": state["agent"],
            "session": state["session"], "status": status, "attempt": state["attempt"],
            "phase": state["phase"],
            "recoveries": state["recoveries"], "max_recoveries": self.request.get("max_recovery_attempts", 5),
            "receipt_file": attempt.get("receipt_file"), "result_file": attempt.get("result_file"),
            "silence_seconds": round(max(0, time.time() - state.get("last_activity_at", time.time())), 1),
            **extra,
        }

    def current_screen(self, session: TmuxSession) -> str:
        # Codex 的输入框定位依赖真实屏幕行号，不能合并终端自动折行。
        return session.capture(join_wrapped=not self.adapter.confirm_submission)

    def dispatch(self, state: JsonObject, session: TmuxSession, text: str) -> None:
        # 在任何输入副作用前保存阶段，控制器中断后不得重放整段任务。
        state.update(observation=None, acknowledged=None)
        if self.adapter.confirm_submission:
            state.update(phase="submitting", submission_stage="paste_started",
                         submission_started_at=time.time(), submission_enter_at=None)
            self.save(state)
            session.send_text(text, submit=False)
            state["submission_stage"] = "paste_sent"
            self.record(state, "task_pasted", message="已粘贴，等待确认输入框后发送 Enter")
        else:
            state["phase"] = "running"
            self.save(state)
            session.send_text(text)
            self.record(state, "task_sent", message="任务已提交到 worker")
        state["last_activity_at"] = time.time()
        self.save(state)

    def snapshot(self, state: JsonObject, screen: str, reason: str) -> JsonObject:
        identity = fingerprint(screen)
        number = state.get("observation_number", 0) + 1
        folder = self.root / "observations"
        folder.mkdir(exist_ok=True)
        path = folder / f"{number:04d}-{reason}.txt"
        try:
            history = self.session.capture(history_lines=HISTORY_LIMIT if reason == "stopped" else DEFAULT_CAPTURE_HISTORY_LINES)
        except TmuxError:
            history = screen
        path.write_text(history, encoding="utf-8")
        observation = {"id": number, "reason": reason, "screen_hash": identity, "snapshot_file": str(path)}
        state.update(observation=observation, observation_number=number)
        silence = round(max(0, time.time() - state.get("last_activity_at", time.time())), 1)
        idle_timeout = self.request.get("worker_idle_timeout_seconds", 300)
        message = reason
        if reason == "review":
            message = f"watch 单次等待结束，交回 DSH 检查；距最近活动 {silence:g}s（静默阈值 {idle_timeout:g}s）"
        elif reason == "silent":
            message = f"连续 {silence:g}s 无活动，达到静默阈值 {idle_timeout:g}s，交回 DSH 判断"
        self.record(state, "needs_attention" if reason not in {"stopped", "review"} else "snapshot",
                    message=message, **observation, silence_seconds=silence,
                    worker_idle_timeout_seconds=idle_timeout, last_activity_at=state.get("last_activity_at"))
        return observation

    def poll(self) -> JsonObject:
        with self.locked() as state:
            if state["phase"] == "stopped":
                return self.response(state, "stopped", reason=state.get("stop_reason"))
            attempt = self.attempt(state)
            receipt_error = None
            if Path(attempt["receipt_file"]).exists():
                try:
                    receipt = WorkerReceipt.load(Path(attempt["receipt_file"]), expected_token=attempt["token"], budget=self.budget)
                    if state["phase"] != "handed_off":
                        state["phase"] = "handed_off"
                        self.record(state, "receipt", message=receipt.status)
                        self.save(state)
                    return self.response(state, "receipt_ready", receipt_status=receipt.status, summary=receipt.summary)
                except RecordError as exc:
                    receipt_error = str(exc)
            try:
                session = self.attached()
                screen = self.current_screen(session)
                activity_status = session.activity_status()
                activity = read_json_object(self.activity_file)
                if (type(activity.get("bytes")) is not int or activity["bytes"] < 0
                        or not isinstance(activity.get("last_output_at"), (int, float))
                        or not math.isfinite(activity["last_output_at"])):
                    raise RecordError("invalid worker activity counter")
                heartbeat_at = 0
                heartbeat_file = self.root / "sdk-heartbeat.json"
                if self.request.get("sdk_heartbeat_counts_as_activity", True) and heartbeat_file.exists():
                    heartbeat_at = read_json_object(heartbeat_file).get("last_output_at")
                    if not isinstance(heartbeat_at, (int, float)) or not math.isfinite(heartbeat_at):
                        raise RecordError("invalid SDK heartbeat timestamp")
                dead = session.status().pane_dead
            except (TmuxError, RecordError) as exc:
                observation = self.snapshot(state, str(exc), "monitor_error")
                self.save(state)
                return self.response(state, "needs_attention", **observation, detail=str(exc))
            now = time.time()
            signature = fingerprint(screen + activity_status)
            # 不归一化 spinner/计时器，也不去重：任何终端字节或界面变化都算活动。
            # 字节记录带真实时间，不能因重新启动 watch 而把旧输出当成刚发生。
            if activity["bytes"] != state["last_bytes"]:
                state["last_activity_at"] = max(state["last_activity_at"], min(now, activity["last_output_at"]))
            elif state["last_screen"] is not None and signature != state["last_screen"]:
                state["last_activity_at"] = now
            state.update(last_screen=signature, last_bytes=activity["bytes"])
            state["last_activity_at"] = max(state["last_activity_at"], min(now, heartbeat_at))
            submission_status = self.adapter.submission_status(screen, activity_status)
            pending = self.adapter.has_pending_submission(screen, activity_status)
            reason = None
            if dead:
                reason = "worker_exited"
            elif not activity_status.endswith(":1"):
                reason = "monitor_error"
            elif receipt_error:
                reason = "invalid_receipt"
            elif self.adapter.is_menu(screen):
                reason = "input_required"
            elif state["phase"] == "submitting":
                stage = state["submission_stage"]
                if pending and stage in {"paste_started", "paste_sent"}:
                    # 已观察到草稿才发第一次 Enter。先持久化，崩溃后不能盲目补发。
                    state.update(submission_stage="enter_sent", submission_enter_at=now)
                    self.save(state)
                    session.send_keys("Enter")
                    self.record(state, "submit_key", message="已发送 Enter，等待输入框清空确认")
                elif stage == "enter_sent" and submission_status == "empty":
                    state["phase"] = "running"
                    self.record(state, "task_sent", message="已确认 Codex 接收输入；完成仍需有效回执")
                elif pending and now - state["submission_enter_at"] >= 2:
                    reason = "submission_pending"
                elif now - state["submission_started_at"] >= 30:
                    reason = "submission_unconfirmed"
            elif pending:
                reason = "submission_pending" if self.adapter.confirm_submission else "input_required"
            # 输入确认优先于活动检测，心跳、spinner 或光标变化不能证明已提交。
            if (reason is None and state["phase"] != "submitting"
                    and self.adapter.error_hint.search(screen)
                    and state.get("acknowledged") != self.attention_key("worker_error", screen)):
                reason = "worker_error"
            elif (reason is None and state["phase"] != "submitting"
                  and now - state["last_activity_at"] >= self.request.get("worker_idle_timeout_seconds", 300)
                  and now >= state.get("observe_until", 0)):
                reason = "silent"
            if (state["phase"] == "starting" and reason is None and self.adapter.is_ready(screen)
                    and (not self.adapter.confirm_submission or submission_status == "empty")
                    and not self.adapter.busy_hint.search(screen)):
                if state["ready_since"] is None:
                    state["ready_since"] = now
                elif now - state["ready_since"] >= 2:
                    self.dispatch(state, session, state["submission"])
            elif state["phase"] == "starting":
                state["ready_since"] = None
            # DSH 可以明确选择继续观察同一错误；仍受静默阈值和全局时限约束。
            key = self.attention_key(reason, screen) if reason else None
            if reason:
                observation = self.snapshot(state, screen, reason)
                state["attention_key"] = key
                self.save(state)
                return self.response(state, "needs_attention", **observation, detail=receipt_error,
                                     screen=re.sub(r"\b[a-f0-9]{48}\b", "[token]", screen[-4000:]))
            if not self.adapter.error_hint.search(screen):
                state["acknowledged"] = None
            self.save(state)
            return self.response(state, state["phase"] if state["phase"] in {"starting", "submitting"} else "running")

    def attention_key(self, reason: str | None, screen: str) -> str:
        if reason == "worker_error":
            return fingerprint("\n".join(line for line in screen.splitlines() if self.adapter.error_hint.search(line)))
        # 选项光标移动不应变成“新的菜单”，相同菜单反复出现要计入恢复预算。
        cleaned = re.sub(r"[❯›►➜✻✽✢✳✶✷]", "", screen)
        cleaned = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ms|s|seconds?|秒)\b|\b\d+:\d+(?::\d+)?\b", "", cleaned, flags=re.I)
        return fingerprint(" ".join(cleaned.split()))

    def pause(self, seconds: float) -> None:
        if self.budget is None:
            time.sleep(seconds)
        else:
            self.budget.sleep(seconds)

    def watch(self, *, wait_seconds: float = 300, acknowledge: int | None = None) -> JsonObject:
        if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 300:
            raise SupervisionError("watch wait must be between 0 and 300 seconds")
        if acknowledge is not None:
            with self.locked() as state:
                self.check_observation(state, acknowledge)
                state["acknowledged"] = state.get("attention_key")
                if state["observation"]["reason"] == "silent":
                    state["observe_until"] = time.time() + wait_seconds
                self.record(state, "observe", message="DSH 决定继续观察，未重置静默计时")
                self.save(state)
        deadline = time.monotonic() + wait_seconds
        while True:
            result = self.poll()
            remaining = deadline - time.monotonic()
            if result["status"] not in {"starting", "submitting", "running"}:
                return result
            if remaining <= 0:
                # 即使 SDK 心跳持续活动，也周期性交回可供 DSH 判断的屏幕证据。
                with self.locked() as state:
                    if state["phase"] == "stopped":
                        return self.response(state, "stopped")
                    screen = self.current_screen(self.attached())
                    observation = self.snapshot(state, screen, "review")
                    self.save(state)
                    return self.response(state, result["status"], **observation,
                                         screen=re.sub(r"\b[a-f0-9]{48}\b", "[token]", screen[-4000:]))
            self.pause(min(10, remaining))

    def check_observation(self, state: JsonObject, number: int) -> None:
        if state["phase"] in {"new", "stopped", "handed_off"}:
            raise SupervisionError("there is no active worker to recover")
        if not state.get("observation") or state["observation"]["id"] != number:
            raise SupervisionError("observation is stale; watch again before acting")
        self.check_receipt(state)

    def check_receipt(self, state: JsonObject) -> None:
        attempt = self.attempt(state)
        if Path(attempt["receipt_file"]).exists():
            try:
                WorkerReceipt.load(Path(attempt["receipt_file"]), expected_token=attempt["token"], budget=self.budget)
            except RecordError:
                pass
            else:
                raise SupervisionError("receipt has arrived; verify it instead of recovering")

    def charge(self, state: JsonObject) -> None:
        if state["recoveries"] >= self.request.get("max_recovery_attempts", 5):
            raise SupervisionError("recovery limit reached; stop and write rejected verdict")
        state["recoveries"] += 1
        state["observation"] = None
        self.save(state)  # 在副作用前扣减；进程退出、命令重试不会绕过额度。

    def recover(self, *, observation: int, reason: str, instruction_file: Path | None = None,
                interrupt: bool = False) -> JsonObject:
        instruction = instruction_file.read_text(encoding="utf-8") if instruction_file else "继续当前任务，先检查已有产物，再完成剩余工作并保存结果和回执。"
        if not reason.strip() or not instruction.strip():
            raise SupervisionError("recovery reason and instruction must not be empty")
        with self.locked() as state:
            self.check_observation(state, observation)
            session = self.attached()
            screen = self.current_screen(session)
            activity_status = session.activity_status()
            if self.adapter.is_menu(screen) or self.adapter.has_pending_submission(screen, activity_status):
                raise SupervisionError("worker is waiting for a choice; use choose")
            if (self.adapter.confirm_submission
                    and self.adapter.submission_status(screen, activity_status) != "empty"
                    and not interrupt):
                raise SupervisionError("cannot confirm an empty composer; inspect before recovering")
            if not interrupt and (not self.adapter.is_ready(screen) or self.adapter.busy_hint.search(screen)):
                raise SupervisionError("worker is still busy; explicitly interrupt or keep watching")
            self.charge(state)
            self.record(state, "recovery", message=reason, interrupt=interrupt)
            if interrupt:
                session.send_keys("C-c")
                self.pause(1)
                screen = self.current_screen(session)
                if (self.adapter.is_menu(screen) or not self.adapter.is_ready(screen)
                        or self.adapter.busy_hint.search(screen)
                        or (self.adapter.confirm_submission
                            and self.adapter.submission_status(screen, session.activity_status()) != "empty")):
                    raise SupervisionError("worker is not ready after interrupt; watch again")
            self.check_receipt(state)
            attempt = self.attempt(state)
            receipt = Path(attempt["receipt_file"])
            if receipt.exists():
                # 保留错误回执证据，由 worker 重新生成；DSH 不代签。
                receipt.rename(receipt.with_name(f"receipt.invalid-{state['recoveries']}.json"))
            self.dispatch(state, session, instruction + "\n\n" + state["submission"])
            state.update(observation=None, acknowledged=None)
            self.save(state)
            return self.response(state, "recovering")

    def choose(self, *, observation: int, keys: list[str], reason: str) -> JsonObject:
        supported_keys = {"Up", "Down", "Left", "Right", "Tab", "Enter", "Escape", "Space", "y", "n", *"0123456789"}
        if not reason.strip() or not keys or len(keys) > 20 or any(k not in supported_keys for k in keys):
            raise SupervisionError("choice requires a reason and at most 20 supported keys")
        with self.locked() as state:
            self.check_observation(state, observation)
            session = self.attached()
            screen = self.current_screen(session)
            if fingerprint(screen) != state["observation"]["screen_hash"]:
                raise SupervisionError("menu changed; watch again before choosing")
            pending = not self.adapter.is_menu(screen) and self.adapter.has_pending_submission(screen, session.activity_status())
            if not (self.adapter.is_menu(screen) or pending):
                raise SupervisionError("no current menu or pending submission")
            if pending and keys != ["Enter"]:
                raise SupervisionError("pending submission requires exactly one Enter, then observe")
            signature = self.attention_key("input_required", screen)
            count = state["choices"].get(signature, 0)
            confirms = any(key in {"Enter", "y", "n", *"0123456789"} for key in keys)
            if confirms and (pending or count or self.adapter.error_hint.search(screen)):
                self.charge(state)
            if confirms:
                state["choices"][signature] = count + 1
            state["observation"] = None
            if pending and self.adapter.confirm_submission:
                state.update(phase="submitting", submission_stage="enter_sent",
                             submission_started_at=time.time(), submission_enter_at=time.time())
            self.save(state)
            self.record(state, "choice", message=reason, keys=keys)
            for key in keys:
                session.send_keys(key)
                self.pause(0.25)
            state.update(last_activity_at=time.time(), observation=None, acknowledged=None)
            self.save(state)
            # 返回真实操作后屏幕，DSH 必须确认选择生效。
        return self.poll()

    def stop(self, *, reason: str) -> JsonObject:
        if not reason.strip():
            raise SupervisionError("stop requires a reason")
        with self.locked() as state:
            if state["phase"] != "stopped":
                capture_error = None
                if self.session.exists():
                    session = self.attached()
                    try:
                        self.snapshot(state, session.capture(), "stopped")
                    except (TmuxError, OSError) as exc:
                        capture_error = str(exc)
                    finally:
                        session.close()
                state.update(phase="stopped", stop_reason=reason)
                self.record(state, "stopped", message=reason, capture_error=capture_error)
                self.save(state)
            return self.response(state, "stopped", reason=reason, observation=state.get("observation"))
