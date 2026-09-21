from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from conductor import Conductor, ConductorConfig, ConductorError, CleanupReport, cleanup_run
from conductor.cli import main
from conductor.errors import OperationError
from conductor.lifecycle import Budget, RunContext, acquire_lock, run_command
from conductor.processes import process_identity, register_process, same_process
from conductor.progress import ProgressReporter
from conductor.state import atomic_write_json
from conductor.tmux import TmuxSession

FIXTURE = Path(__file__).parent / "fixtures" / "fake_dsh.py"


def config(**env: str) -> ConductorConfig:
    return ConductorConfig(dsh_bin=str(FIXTURE), timeout_seconds=4, worker_log=False,
                           dsh_extra_env={"FAKE_DSH_SDK": "1", **env})


def wait_for_file(path: Path, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"fixture did not create {path}")
        time.sleep(0.01)


class DeadlineTests(unittest.TestCase):
    def test_cleanup_reserve_and_child_deadlines_do_not_restart_budget(self):
        with mock.patch("conductor.lifecycle.time.monotonic", return_value=100):
            context = RunContext(600, 5)
            self.assertEqual(context.total_deadline, 700)
            self.assertEqual(context.execution_deadline, 695)
            short = RunContext(2, 5)
            self.assertAlmostEqual(short.execution_deadline, 101.8)
        with mock.patch("conductor.lifecycle.time.monotonic", return_value=694):
            self.assertEqual(context.budget().limit(30).remaining(), 1)
            self.assertEqual(context.cleanup_budget().deadline, 699)
        with mock.patch("conductor.lifecycle.time.monotonic", return_value=695):
            with self.assertRaises(OperationError) as caught:
                context.budget().remaining()
            self.assertEqual(caught.exception.details["timeout_scope"], "total")

    def test_cleanup_budget_ignores_cancellation(self):
        cancel = threading.Event()
        context = RunContext(3, 1, cancel)
        cancel.set()
        with self.assertRaises(OperationError):
            context.budget().check()
        context.cleanup_budget().check()
        self.assertTrue(cancel.is_set())

    def test_dsh_shutdown_stage_caps_the_shared_cleanup_budget(self):
        from conductor import DshClient, DshConfig

        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            client = DshClient(DshConfig(workspace=Path(directory), dsh_bin=str(FIXTURE),
                                        shutdown_timeout_seconds=0.1,
                                        extra_env={"FAKE_DSH_IGNORE_TERM": "1", "FAKE_DSH_READY_FILE": str(ready)}))
            try:
                client.start()
                wait_for_file(ready)
                started = time.monotonic()
                client.close(budget=Budget(started + 2, "cleanup"), graceful=False)
                self.assertLess(time.monotonic() - started, 0.3)
                self.assertIsNotNone(client._process.poll())
            finally:
                client.close(graceful=False)

    def test_lock_wait_and_blocked_command_obey_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            with path.open("a") as first, path.open("a") as second:
                acquire_lock(first, Budget(time.monotonic() + 1))
                started = time.monotonic()
                with self.assertRaises(OperationError) as caught:
                    acquire_lock(second, Budget(started + 0.15))
                self.assertEqual(caught.exception.code, "timeout")
                self.assertLess(time.monotonic() - started, 0.5)
        started = time.monotonic()
        with self.assertRaises(OperationError) as caught:
            run_command([sys.executable, "-c", "import time; time.sleep(30)"],
                        budget=Budget(started + 0.15))
        self.assertEqual(caught.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 0.5)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        # fake DSH 不依赖真实模型；tmux subprocess 仍使用真正的 tmux。
        self.patch = mock.patch("conductor.skills.shutil.which", return_value="/bin/true")
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_pre_cancel_creates_no_run_resources(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ConductorError) as caught, mock.patch("conductor.sdk.DshClient") as client:
            Conductor(self.workspace, config()).run("task", cancel_event=cancel)
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertEqual(caught.exception.phase, "validation")
        self.assertEqual(caught.exception.cleanup.status, "completed")
        self.assertIsNone(caught.exception.run_id)
        self.assertEqual(list(self.workspace.iterdir()), [])
        client.assert_not_called()
        self.assertTrue(cancel.is_set())

    def test_cancel_during_preparation_or_validation_keeps_actual_phase(self):
        from conductor.skills import prepare_workspace_skills
        from conductor.models import Verdict
        for phase in ("preparation", "result_validation"):
            cancel = threading.Event()
            original = prepare_workspace_skills if phase == "preparation" else Verdict.load

            def cancel_here(*args, **kwargs):
                if phase == "preparation":
                    result = original(*args, **kwargs)
                    cancel.set()
                    return result
                cancel.set()
                return original(*args, **kwargs)

            target = "conductor.sdk.prepare_workspace_skills" if phase == "preparation" else "conductor.sdk.Verdict.load"
            with self.subTest(phase=phase), mock.patch(target, side_effect=cancel_here):
                with self.assertRaises(ConductorError) as caught:
                    Conductor(self.workspace, config()).run("task", cancel_event=cancel)
                self.assertEqual(caught.exception.code, "cancelled")
                self.assertEqual(caught.exception.phase, phase)
                self.assertEqual(caught.exception.cleanup.status, "completed")

    def test_initialization_cancel_is_prompt_and_reaps_process(self):
        ready = self.workspace / "ready"
        cancel = threading.Event()
        errors = []

        def run():
            try:
                Conductor(self.workspace, config(FAKE_DSH_INIT_DELAY="30", FAKE_DSH_READY_FILE=str(ready))).run("task", cancel_event=cancel)
            except ConductorError as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            wait_for_file(ready)
            identity = process_identity(int(ready.read_text()))
            started = time.monotonic()
            cancel.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(errors[0].code, "cancelled")
            self.assertEqual(errors[0].phase, "dsh_initialize")
            self.assertEqual(errors[0].cleanup.status, "completed")
            self.assertFalse(same_process(identity, process_identity(identity["pid"])))
        finally:
            cancel.set()
            thread.join(5)

    def test_worker_cancel_releases_supervision_lock_and_ignores_keep_session(self):
        ready = self.workspace / "worker-ready"
        cancel = threading.Event()
        errors = []
        cfg = ConductorConfig(dsh_bin=str(FIXTURE), timeout_seconds=10, keep_session=True, worker_log=False,
                              dsh_extra_env={"FAKE_DSH_SDK": "1", "FAKE_DSH_WORKER": "1", "FAKE_DSH_NO_TURN": "1",
                                             "FAKE_DSH_HOLD_LOCK": "1", "FAKE_DSH_WORKER_READY": str(ready)})

        def run():
            try:
                Conductor(self.workspace, cfg).run("task", cancel_event=cancel)
            except ConductorError as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            wait_for_file(ready)
            time.sleep(0.15)  # 让真实锁持有者进入等待。
            cancel.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            error = errors[0]
            self.assertEqual(error.code, "cancelled")
            self.assertEqual(error.cleanup.status, "completed", error.cleanup)
            runtime = read_runtime(error.state_directory)
            self.assertFalse(TmuxSession(ready.read_text(), socket_path=Path(runtime["tmux_socket"])).exists())
        finally:
            cancel.set()
            thread.join(5)

    def test_stage_and_total_timeout_include_initialization_and_prompt(self):
        cases = [("initialization", {"FAKE_DSH_INIT_DELAY": "30"}, 0.15, "stage", "dsh_initialize"),
                 ("prompt", {"FAKE_DSH_INIT_DELAY": "0.15", "FAKE_DSH_PROMPT_DELAY": "0.15", "FAKE_DSH_NO_TURN": "1"}, 30, "total", "dsh_run")]
        for name, env, init_timeout, scope, phase in cases:
            with self.subTest(name=name):
                cfg = ConductorConfig(dsh_bin=str(FIXTURE), timeout_seconds=0.65, dsh_init_timeout_seconds=init_timeout,
                                      worker_log=False, dsh_extra_env=env)
                started = time.monotonic()
                with self.assertRaises(ConductorError) as caught:
                    Conductor(self.workspace, cfg).run("task")
                self.assertEqual(caught.exception.code, "timeout")
                self.assertEqual(caught.exception.phase, phase)
                self.assertEqual(caught.exception.details["timeout_scope"], scope)
                self.assertLess(time.monotonic() - started, 0.9)

    def test_large_prompt_write_can_be_cancelled_or_timed_out(self):
        from conductor.dsh import DshClient, DshConfig, DshError

        # 直接向已初始化的 DSH 写入大帧：SDK 原始 prompt 会落盘，实际 RPC 只传管理指令。
        # 计时从待验证的写入阶段开始，避免慢 runner 在准备阶段就取消而根本没有测到管道写入。
        prompt = "x" * 2_000_000
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                cancel = threading.Event()
                timer = threading.Timer(0.2, cancel.set) if cancelled else None
                client = DshClient(DshConfig(workspace=self.workspace, dsh_bin=str(FIXTURE),
                                             extra_env={"FAKE_DSH_FREEZE_STDIN": "1"},
                                             budget=Budget(float("inf"), cancel_event=cancel)))
                try:
                    client.start()
                    client.initialize()
                    if timer:
                        timer.start()
                    started = time.monotonic()
                    with self.assertRaises(DshError) as caught:
                        client.run(prompt, session_id="large-write", timeout_seconds=0.5)
                    self.assertEqual(caught.exception.code, "cancelled" if cancelled else "timeout")
                    self.assertEqual(caught.exception.details["rpc_method"], "session/prompt")
                    self.assertLess(time.monotonic() - started, 2)
                finally:
                    if timer:
                        timer.cancel()
                        timer.join()
                    client.close(budget=Budget(time.monotonic() + 2), graceful=False)

    def test_invalid_protocol_and_incomplete_completion_cannot_succeed(self):
        for env, code in (({"FAKE_DSH_INVALID_FRAME": "1"}, "dsh_protocol_error"),
                          ({"FAKE_DSH_NO_IDLE": "1"}, "dsh_process_exited"),
                          ({"FAKE_DSH_RPC_ERROR": "1"}, "dsh_rpc_failed")):
            with self.subTest(code=code), self.assertRaises(ConductorError) as caught:
                Conductor(self.workspace, config(**env)).run("task")
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.cleanup.status, "completed")
            self.assertIsNotNone(caught.exception.__cause__)

    def test_exited_dsh_child_holding_pipes_and_ignoring_term_is_killed(self):
        child_path = self.workspace / "child"
        started = time.monotonic()
        with self.assertRaises(ConductorError) as caught:
            Conductor(self.workspace, config(FAKE_DSH_EXIT_WITH_CHILD="1", FAKE_DSH_CHILD_PID=str(child_path))).run("task")
        self.assertEqual(caught.exception.code, "dsh_process_exited")
        self.assertEqual(caught.exception.cleanup.status, "completed", caught.exception.cleanup)
        child = process_identity(int(child_path.read_text()))
        self.assertTrue(child is None or child["state"] == "Z")
        self.assertLess(time.monotonic() - started, 2)

    def test_cleanup_failure_preserves_primary_error_or_verified_result(self):
        incomplete = CleanupReport("incomplete", remaining_resources=({"kind": "fixture"},))
        with mock.patch("conductor.sdk.cleanup_resources", return_value=incomplete):
            with self.assertRaises(ConductorError) as caught:
                Conductor(self.workspace, config()).run("task")
            self.assertEqual(caught.exception.code, "cleanup_failed")
            self.assertTrue(caught.exception.result.accepted)
            self.assertTrue((caught.exception.state_directory / "verdict.json").exists())
            payload = caught.exception.to_json()
            self.assertEqual(payload["result"]["status"], "accepted")
            cfg = ConductorConfig(dsh_bin=str(FIXTURE), worker_log=False, timeout_seconds=0.3,
                                  dsh_extra_env={"FAKE_DSH_INIT_DELAY": "30"})
            with self.assertRaises(ConductorError) as caught:
                Conductor(self.workspace, cfg).run("task")
            self.assertEqual(caught.exception.code, "timeout")
            self.assertEqual(caught.exception.cleanup.status, "incomplete")
        for path in (self.workspace / ".dsh-conductor" / "runs").iterdir():
            self.assertEqual(cleanup_run(path).status, "completed")

    def test_slow_callback_does_not_prevent_cleanup(self):
        release = threading.Event()
        entered = threading.Event()

        def callback(event):
            entered.set()
            release.wait(10)

        started = time.monotonic()
        try:
            result = Conductor(self.workspace, config()).run("task", callback)
            self.assertTrue(entered.is_set())
            self.assertTrue(result.accepted)
            self.assertEqual(result.cleanup.status, "completed")
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertTrue(any("callback" in message for message in result.cleanup.errors))
        finally:
            release.set()

    def test_same_workspace_is_rejected_without_touching_active_run(self):
        ready = self.workspace / "ready"
        cancel = threading.Event()
        errors = []

        def run():
            try:
                Conductor(self.workspace, config(FAKE_DSH_INIT_DELAY="30", FAKE_DSH_READY_FILE=str(ready))).run("task", cancel_event=cancel)
            except ConductorError as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            wait_for_file(ready)
            with self.assertRaises(ConductorError) as caught:
                Conductor(self.workspace, config()).run("other")
            self.assertEqual(caught.exception.code, "workspace_busy")
            root = next((self.workspace / ".dsh-conductor" / "runs").iterdir())
            self.assertEqual(cleanup_run(root).status, "incomplete")
            self.assertFalse((root / "sdk-stop.json").exists())
        finally:
            cancel.set()
            thread.join(5)
        self.assertEqual(errors[0].code, "cancelled")

    def test_keyboard_interrupt_cleans_up_and_keeps_propagation(self):
        for exception in (KeyboardInterrupt, SystemExit):
            with mock.patch("conductor.sdk.ExecutionPlan.load", side_effect=exception):
                with self.assertRaises(exception):
                    Conductor(self.workspace, config()).run("task")
        for root in (self.workspace / ".dsh-conductor" / "runs").iterdir():
            self.assertEqual(read_runtime(root)["cleanup"]["status"], "completed")

    def test_callback_failure_and_late_cancel_do_not_change_result_or_signals(self):
        handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        cancel = threading.Event()

        def callback(event):
            raise RuntimeError("display failed")

        result = Conductor(self.workspace, config()).run("task", callback, cancel_event=cancel)
        before = result.to_json()
        cancel.set()
        self.assertTrue(result.accepted)
        self.assertEqual(result.to_json(), before)
        self.assertEqual(handlers, {s: signal.getsignal(s) for s in handlers})

    def test_partial_preparation_failure_has_run_identity_and_no_process(self):
        from conductor.state import atomic_write_json as original

        def fail_request(path, value):
            if path.name == "request.json":
                raise OSError("fixture disk failure")
            return original(path, value)

        with mock.patch("conductor.state.atomic_write_json", side_effect=fail_request):
            with self.assertRaises(ConductorError) as caught:
                Conductor(self.workspace, config()).run("task")
        self.assertEqual(caught.exception.code, "preparation_failed")
        self.assertIsNotNone(caught.exception.run_id)
        self.assertTrue(caught.exception.state_directory.is_dir())
        self.assertEqual(caught.exception.cleanup.status, "completed")

    def test_partial_reporter_start_failure_stops_started_dispatcher(self):
        original = threading.Thread.start
        started = []

        def start(thread):
            if thread.name == "dsh-heartbeat":
                raise RuntimeError("fixture cannot start heartbeat thread")
            started.append(thread)
            return original(thread)

        with mock.patch("threading.Thread.start", start):
            with self.assertRaises(ConductorError) as caught:
                Conductor(self.workspace, config()).run("task", lambda event: None)
        self.assertEqual(caught.exception.code, "internal_error")
        self.assertEqual(caught.exception.cleanup.status, "completed")
        self.assertTrue(all(not thread.is_alive() for thread in started))


def read_runtime(root: Path):
    return json.loads((root / "runtime.json").read_text())


class RecoveryTests(unittest.TestCase):
    def test_cleanup_after_sdk_process_is_killed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "worker-ready"
            code = (
                "from conductor import Conductor, ConductorConfig; "
                f"Conductor({directory!r}, ConductorConfig(dsh_bin={str(FIXTURE.resolve())!r}, "
                "timeout_seconds=60, worker_log=False, dsh_extra_env="
                f"{{'FAKE_DSH_SDK':'1','FAKE_DSH_WORKER':'1','FAKE_DSH_NO_TURN':'1', 'FAKE_DSH_WORKER_READY':{str(ready)!r}}})).run('test')"
            )
            owner = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            run_root = None
            try:
                wait_for_file(ready)
                run_root = next((root / ".dsh-conductor" / "runs").iterdir())
                runtime = read_runtime(run_root)
                owner.kill()
                owner.wait()
                report = cleanup_run(run_root)
                self.assertEqual(report.status, "completed", report)
                self.assertFalse(TmuxSession(ready.read_text(), socket_path=Path(runtime["tmux_socket"])).exists())
                self.assertTrue((run_root / "request.json").exists())
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait()
                if run_root is not None:
                    cleanup_run(run_root)

    def test_retained_run_cleanup_is_idempotent_and_does_not_touch_other_sockets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = TmuxSession.create(name=f"dsh-isolation-{time.monotonic_ns()}", workspace=root,
                                        agent="fixture", command=["sleep", "60"])
            results = []
            try:
                for name in ("first", "second"):
                    workspace = root / name
                    workspace.mkdir()
                    cfg = ConductorConfig(dsh_bin=str(FIXTURE), timeout_seconds=10, keep_session=True, worker_log=False,
                                          dsh_extra_env={"FAKE_DSH_SDK": "1", "FAKE_DSH_WORKER": "1"})
                    with mock.patch("conductor.skills.shutil.which", return_value="/bin/true"):
                        results.append(Conductor(workspace, cfg).run("task"))
                first, second = results
                self.assertNotEqual(first.tmux_socket, second.tmux_socket)
                activity_records = list((second.state_directory / "resources").glob("activity-*.json"))
                self.assertTrue(activity_records)
                for path in activity_records:
                    expected = json.loads(path.read_text())["identity"]
                    actual = process_identity(expected["pid"])
                    self.assertTrue(not same_process(expected, actual) or actual["state"] == "Z")
                for _ in range(2):
                    self.assertEqual(cleanup_run(first.state_directory).status, "completed")
                self.assertTrue(TmuxSession(second.session, socket_path=second.tmux_socket).exists())
                self.assertTrue(anchor.exists())
                self.assertTrue((first.state_directory / "verdict.json").exists())
            finally:
                for result in results:
                    cleanup_run(result.state_directory)
                anchor.close()

    def test_retention_only_preserves_the_selected_worker_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            cfg = ConductorConfig(dsh_bin=str(FIXTURE), timeout_seconds=10, keep_session=True, worker_log=False,
                                  dsh_extra_env={"FAKE_DSH_SDK": "1", "FAKE_DSH_WORKER": "1", "FAKE_DSH_EXTRA_WORKER": "1"})
            result = None
            try:
                with mock.patch("conductor.skills.shutil.which", return_value="/bin/true"):
                    result = Conductor(workspace, cfg).run("task")
                self.assertEqual(result.cleanup.status, "retained", result.cleanup)
                selected_pid = TmuxSession(result.session, socket_path=result.tmux_socket).status().pane_pid
                workers = [json.loads(path.read_text())["identity"]
                           for path in (result.state_directory / "resources").glob("worker-*.json")]
                self.assertEqual(len(workers), 2)
                for expected in workers:
                    actual = process_identity(expected["pid"])
                    alive = same_process(expected, actual) and actual["state"] != "Z"
                    self.assertEqual(alive, expected["pid"] == selected_pid)
            finally:
                if result is not None:
                    cleanup_run(result.state_directory)

    def test_reused_pid_is_not_signalled_and_missing_metadata_is_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch("conductor.skills.shutil.which", return_value="/bin/true"):
                result = Conductor(root, config()).run("task")
            child = subprocess.Popen(["sleep", "60"], start_new_session=True)
            try:
                record = register_process(child.pid, "dsh", result.state_directory / "runtime.json")
                record["identity"]["start_time"] = "0"
                record["members"] = []
                atomic_write_json(Path(record["record_file"]), record)
                report = cleanup_run(result.state_directory)
                self.assertEqual(report.status, "completed", report)
                self.assertIsNone(child.poll())
                self.assertTrue(any("reused" in message for message in report.errors))
            finally:
                child.kill()
                child.wait()
            (result.state_directory / "runtime.json").unlink()
            self.assertEqual(cleanup_run(result.state_directory).status, "incomplete")

    def test_incomplete_process_identity_is_reported_without_signalling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch("conductor.skills.shutil.which", return_value="/bin/true"):
                result = Conductor(root, config()).run("task")
            child = subprocess.Popen(["sleep", "60"], start_new_session=True)
            try:
                record = register_process(child.pid, "dsh", result.state_directory / "runtime.json")
                for invalid in ({"identity": {}}, {"identity": {"pid": child.pid}}, {"members": [None]}):
                    with self.subTest(invalid=invalid):
                        atomic_write_json(Path(record["record_file"]), {**record, **invalid})
                        report = cleanup_run(result.state_directory)
                        self.assertEqual(report.status, "incomplete", report)
                        self.assertTrue(any(item.get("reason") == "ownership_unverified"
                                            for item in report.remaining_resources), report)
                        self.assertIsNone(child.poll())
            finally:
                child.kill()
                child.wait()


class CliLifecycleTests(unittest.TestCase):
    def test_argument_errors_are_json_and_restore_signal_handlers(self):
        handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
        for args, expected in ((["run"], "invalid_input"),
                               (["run", "--workspace", "/tmp", "--prompt", "test", "--timeout-seconds", "nan"], "invalid_config")):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(args), 1)
            self.assertEqual(json.loads(output.getvalue())["code"], expected)
        self.assertEqual(handlers, {signum: signal.getsignal(signum) for signum in handlers})

    def test_signals_produce_one_json_after_cleanup(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as directory:
                ready = Path(directory) / "ready"
                env = {**os.environ, "FAKE_DSH_INIT_DELAY": "30", "FAKE_DSH_READY_FILE": str(ready)}
                process = subprocess.Popen([sys.executable, "-m", "conductor", "run", "--workspace", directory,
                                            "--prompt", "test", "--dsh-bin", str(FIXTURE), "--no-worker-log"],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                try:
                    wait_for_file(ready)
                    process.send_signal(signum)
                    stdout, stderr = process.communicate(timeout=5)
                    payload = json.loads(stdout)
                    self.assertEqual(process.returncode, 128 + signum, stderr)
                    self.assertEqual(payload["code"], "cancelled")
                    self.assertEqual(payload["cleanup"]["status"], "completed")
                    self.assertEqual(payload["details"]["signal"], signum)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()


class CallbackTests(unittest.TestCase):
    def test_log_write_lock_obeys_the_cleanup_deadline(self):
        from conductor.worker_log import WorkerLogFollower, _LOG_WRITE_LOCK

        with tempfile.TemporaryDirectory() as directory:
            follower = WorkerLogFollower(session_name="fixture", agent="fixture", log_file=Path(directory) / "log",
                                         sink=lambda *args: None)
            entered = threading.Event()

            def hold_lock():
                with _LOG_WRITE_LOCK:
                    entered.set()
                    time.sleep(0.5)

            holder = threading.Thread(target=hold_lock)
            holder.start()
            try:
                self.assertTrue(entered.wait(1))
                session = mock.Mock()
                session.exists.return_value = True
                session.capture.return_value = "new worker output"
                started = time.monotonic()
                with mock.patch("conductor.worker_log.TmuxSession", return_value=session):
                    with self.assertRaises(OperationError) as caught:
                        follower.sample_once(budget=Budget(started + 0.1, "cleanup"))
                self.assertEqual(caught.exception.code, "timeout")
                self.assertLess(time.monotonic() - started, 0.3)
            finally:
                holder.join(1)

    def test_dsh_low_level_callback_cannot_block_protocol_or_close(self):
        from conductor import DshClient, DshConfig
        release = threading.Event()
        entered = threading.Event()

        def callback(event):
            entered.set()
            release.wait(10)

        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            try:
                with DshClient(DshConfig(workspace=Path(directory), dsh_bin=str(FIXTURE))) as client:
                    result = client.run("test", session_id="test", timeout_seconds=1, on_event=callback)
                self.assertTrue(entered.is_set())
                self.assertEqual(result.status, "completed")
                self.assertLess(time.monotonic() - started, 0.6)
            finally:
                release.set()

    def test_log_stop_interrupts_capture_and_does_not_wait_for_next_interval(self):
        from conductor.worker_log import WorkerLogFollower
        entered = threading.Event()

        class HangingSession:
            def __init__(self, name, *, budget, **kwargs):
                self.budget = budget

            def exists(self):
                return True

            def capture(self, **kwargs):
                entered.set()
                return run_command([sys.executable, "-c", "import time; time.sleep(30)"], budget=self.budget).stdout

        with tempfile.TemporaryDirectory() as directory, mock.patch("conductor.worker_log.TmuxSession", HangingSession):
            follower = WorkerLogFollower(session_name="fixture", agent="fixture", log_file=Path(directory) / "log",
                                         sink=lambda *args: None, interval_seconds=100).start()
            try:
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                follower.stop(budget=Budget(started + 0.3, "cleanup"))
                self.assertLess(time.monotonic() - started, 0.6)
                self.assertFalse(follower.is_alive)
            finally:
                follower.stop()

    def test_backlog_is_bounded_and_shutdown_does_not_wait_for_user_code(self):
        release = threading.Event()
        entered = threading.Event()

        def callback(event):
            entered.set()
            release.wait(10)

        reporter = ProgressReporter(on_event=callback, max_pending_events=4).start()
        try:
            reporter.emit(source="test", kind="test", message="first")
            self.assertTrue(entered.wait(1))
            for i in range(100):
                reporter.emit(source="test", kind="test", message=str(i))
            self.assertLessEqual(reporter._events.qsize(), 4)
            self.assertGreaterEqual(reporter.dropped_events, 96)
            started = time.monotonic()
            reporter.stop(budget=Budget(started + 0.15, "cleanup"))
            self.assertLess(time.monotonic() - started, 0.4)
            self.assertTrue(reporter.callback_inflight)
            self.assertEqual(reporter._events.qsize(), 0)
        finally:
            release.set()
