from __future__ import annotations

import shutil
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from conductor.agents.base import main
from conductor.agents.claude import CLAUDE
from conductor.agents.codex import CODEX
from conductor.tmux import DEFAULT_CAPTURE_HISTORY_LINES, HISTORY_LIMIT, TmuxError, TmuxSession, _run
from conductor.models import AgentKind
from conductor.state import RunState, atomic_write_json
from conductor.supervision import Supervisor


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TmuxTransportTests(unittest.TestCase):
    def test_existing_server_receives_current_package_and_python_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = TmuxSession.create(name=f'dsh-env-anchor-{time.monotonic_ns()}', workspace=root,
                                        agent='fixture', command=['sleep', '30'])
            session = None
            try:
                modules = root / 'modules'
                modules.mkdir()
                (modules / 'worker_import_probe.py').write_text('VALUE = "current environment"\n')
                output = root / 'import.txt'
                code = "import pathlib,conductor,worker_import_probe; pathlib.Path('import.txt').write_text(conductor.__version__ + ':' + worker_import_probe.VALUE)"
                with mock.patch.dict('os.environ', {'PYTHONPATH': str(modules)}):
                    session = TmuxSession.create(name=f'dsh-env-test-{time.monotonic_ns()}', workspace=root,
                                                 agent='fixture', command=[sys.executable, '-c', code])
                deadline = time.monotonic() + 5
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                from conductor import __version__
                self.assertEqual(output.read_text(), __version__ + ':current environment')
            finally:
                if session is not None:
                    session.close()
                anchor.close()

    def test_missing_session_does_not_match_or_close_another_sessions_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefix = f'dsh-exact-{time.monotonic_ns()}'
            session = TmuxSession.create(name=prefix + '-extra', workspace=Path(directory),
                                         agent='fixture', command=['sleep', '30'])
            try:
                absent = TmuxSession(prefix)
                self.assertFalse(absent.exists())
                absent.close()
                self.assertTrue(session.exists())
                with self.assertRaises(TmuxError):
                    absent.capture()
                with self.assertRaises(TmuxError):
                    absent.send_keys('Enter')
            finally:
                session.close()

    def test_capture_and_input_stay_on_worker_pane_after_switching_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'input.txt'
            code = "import pathlib,sys; print('WORKER', flush=True); pathlib.Path(sys.argv[1]).write_text(input())"
            session = TmuxSession.create(name=f'dsh-pane-{time.monotonic_ns()}', workspace=root,
                                         agent='fixture', command=[sys.executable, '-c', code, str(output)])
            try:
                _run(['new-window', '-t', session.target, '-n', 'observer', 'exec sleep 30'])
                deadline = time.monotonic() + 5
                while 'WORKER' not in session.capture() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIn('WORKER', session.capture())
                session.send_text('targeted worker input', submit_delay_seconds=0)
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(output.read_text(), 'targeted worker input')
            finally:
                session.close()

    def test_pipe_records_identical_output_and_cursor_control_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            activity = root / 'activity.json'
            code = "import sys,time; print('tick', flush=True); time.sleep(.3); sys.stdout.write('\\x1b[?25l\\x1b[?25h\\r'); sys.stdout.flush(); time.sleep(30)"
            session = TmuxSession.create(name=f'dsh-activity-{time.monotonic_ns()}', workspace=root,
                                         agent='fixture', command=[sys.executable, '-c', code], activity_file=activity)
            try:
                deadline = time.monotonic() + 5
                while json.loads(activity.read_text())['bytes'] < 18 and time.monotonic() < deadline:
                    time.sleep(.02)
                record = json.loads(activity.read_text())
                self.assertGreaterEqual(record['bytes'], 18)
                self.assertGreater(record['last_output_at'], 0)
                self.assertTrue(session.activity_status().endswith(':1'))
                self.assertEqual(session.capture().strip(), 'tick')
            finally:
                session.close()

    def test_both_adapters_complete_menu_recovery_receipt_flow_in_real_tmux(self) -> None:
        for adapter in (CLAUDE, CODEX):
            with self.subTest(agent=adapter.kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run = RunState.create(state_root=root / 'state', workspace=root, prompt='test',
                                      available_agents=set(AgentKind), max_attempts=1,
                                      worker_idle_timeout_seconds=300, keep_session=False)
                selected = run.agent_state(AgentKind(adapter.kind))
                attempt = selected.attempts[0]
                attempt.task_file.write_text('task')
                atomic_write_json(run.plan_file, {
                    'schema_version': 1, 'run_id': run.run_id, 'agent': adapter.kind, 'agent_reason': 'test',
                    'task_summary': 'test', 'implementation_steps': ['test'],
                    'acceptance_criteria': [{'id': 'test', 'description': 'test'}],
                })
                script = Path(__file__).parent / 'fixtures' / 'worker_tui.py'
                payload = '<dsh_conductor_handoff>\n中文任务 literal $HOME $(date)\n</dsh_conductor_handoff>'
                supervisor = Supervisor(run.request_file, adapter, selected.session)

                def wait_for(reason: str):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        value = supervisor.poll()
                        if value.get('reason') == reason or value['status'] == reason:
                            return value
                        time.sleep(.05)
                    self.fail(f'never observed {reason}: {value}')

                try:
                    start = supervisor.start(task_file=attempt.task_file, receipt_file=attempt.receipt_file,
                                             token=attempt.token, submission=payload,
                                             command=[sys.executable, str(script), adapter.cursor_glyphs[0],
                                                      str(attempt.receipt_file.parent), attempt.token,
                                                      *(['--swallow-enter'] if adapter.kind == 'codex' else [])])
                    self.assertEqual(start['status'], 'starting')
                    menu = wait_for('input_required')
                    supervisor.choose(observation=menu['id'], keys=['Enter'], reason='继续启动')
                    if adapter.kind == 'codex':
                        pending = wait_for('submission_pending')
                        self.assertEqual(pending['phase'], 'submitting')
                        self.assertFalse((attempt.receipt_file.parent / 'received-1.txt').exists())
                        self.assertEqual((attempt.receipt_file.parent / 'draft-1.txt').read_text(), payload)
                        supervisor.choose(observation=pending['id'], keys=['Enter'], reason='补发未提交的任务')
                    failure = wait_for('worker_error')
                    self.assertEqual((attempt.receipt_file.parent / 'received-1.txt').read_text().rstrip(), payload)
                    supervisor.recover(observation=failure['id'], reason='网络恢复后继续')
                    finished = wait_for('receipt_ready')
                    self.assertEqual(finished['recoveries'], 2 if adapter.kind == 'codex' else 1)
                    self.assertTrue((attempt.receipt_file.parent / 'received-2.txt').read_text().endswith(payload))
                    self.assertEqual(attempt.result_file.read_text(), 'Complete UTF-8 report')
                    self.assertEqual(supervisor.stop(reason='test completed')['status'], 'stopped')
                    self.assertFalse(TmuxSession(selected.session).exists())
                finally:
                    TmuxSession(selected.session).close()

    def test_long_output_uses_actual_history_limit_and_expanded_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for adapter in (CLAUDE, CODEX):
                session_name = f"dsh-long-test-{time.monotonic_ns()}"
                count = 6000
                code = f"import sys,time; sys.stdout.write('\\n'.join(f'ROW-{{i:05d}}' for i in range({count}))); sys.stdout.flush(); time.sleep(30)"
                session = TmuxSession.create(
                    name=session_name, workspace=root, agent=adapter.kind,
                    command=[sys.executable, "-c", code],
                )
                try:
                    deadline = time.monotonic() + 5
                    while "ROW-05999" not in session.capture() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertIn("ROW-05999", session.capture())
                    actual = _run(["display-message", "-p", "-t", session_name, "#{history_limit}"]).stdout.strip()
                    self.assertEqual(int(actual), HISTORY_LIMIT)
                    self.assertEqual(len(session.capture(history_lines=HISTORY_LIMIT).splitlines()), count)
                    # 本地 tmux 的 window-size 配置或已连接客户端可能改变实际高度。
                    height = int(_run(["display-message", "-p", "-t", session.target, "#{pane_height}"]).stdout.strip())
                    for history, expected_count in ((None, DEFAULT_CAPTURE_HISTORY_LINES + height), (0, height), (HISTORY_LIMIT, count)):
                        with self.subTest(agent=adapter.kind, history=history):
                            args = ["capture", "--session", session_name]
                            if history is not None:
                                args.extend(["--history-lines", str(history)])
                            output = io.StringIO()
                            with redirect_stdout(output):
                                self.assertEqual(main(adapter, args), 0)
                            screen = json.loads(output.getvalue())["screen"]
                            self.assertEqual(len(screen.splitlines()), expected_count)
                            self.assertIn("ROW-05999", screen)
                finally:
                    session.close()

    def test_buffer_transport_preserves_literal_text_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "input.txt"
            session_name = f"dsh-test-{time.monotonic_ns()}"[-60:]
            code = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(input(), encoding='utf-8')"
            session = TmuxSession.create(
                name=session_name,
                workspace=root,
                agent="fixture",
                command=[sys.executable, "-c", code, str(output)],
            )
            try:
                payload = "literal $HOME $(date) Enter 'quoted'"
                session.send_text(payload, submit_delay_seconds=0)
                deadline = time.monotonic() + 5
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(output.read_text(encoding="utf-8"), payload)
                status = session.status()
                self.assertEqual(status.agent, "fixture")
                self.assertEqual(status.workspace, str(root.resolve()))
                with self.assertRaises(TmuxError):
                    TmuxSession.attach(name=session_name, expected_agent="other")
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
