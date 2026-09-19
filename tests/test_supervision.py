from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from concurrent.futures import ThreadPoolExecutor

from conductor.agents.codex import CODEX
from conductor.models import AgentKind
from conductor.state import RunState, atomic_write_json
from conductor.supervision import Supervisor, SupervisionError
from conductor.tmux import HISTORY_LIMIT, TmuxError


class SupervisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = RunState.create(state_root=self.root / 'state', workspace=self.root, prompt='test',
                                   available_agents=set(AgentKind), max_attempts=2,
                                   worker_idle_timeout_seconds=300, keep_session=True)
        self.agent = self.run.agent_state(AgentKind.CODEX)
        atomic_write_json(self.run.plan_file, {
            'schema_version': 1, 'run_id': self.run.run_id, 'agent': 'codex', 'agent_reason': 'test',
            'task_summary': 'test', 'implementation_steps': ['test'],
            'acceptance_criteria': [{'id': 'test', 'description': 'test'}],
        })
        self.session = mock.Mock(name='session')
        self.session.name = self.agent.session
        self.session.capture.return_value = '› Ask Codex to do anything'
        self.session.activity_status.return_value = '2:0:1::1'
        self.session.status.return_value = SimpleNamespace(workspace=str(self.root), pane_dead=False)
        factory = mock.patch('conductor.supervision.TmuxSession').start()
        self.addCleanup(mock.patch.stopall)
        factory.return_value = self.session
        factory.attach.return_value = self.session
        factory.create.return_value = self.session
        self.factory = factory
        self.now = 1000.0
        mock.patch('conductor.supervision.time.time', side_effect=lambda: self.now).start()
        mock.patch('conductor.supervision.time.sleep').start()
        self.supervisor = self.new_supervisor()
        self.activity(0)
        self.start(1)

    def new_supervisor(self) -> Supervisor:
        return Supervisor(self.run.request_file, CODEX, self.agent.session)

    def start(self, number: int) -> None:
        attempt = self.agent.attempts[number - 1]
        attempt.task_file.write_text('build and verify', encoding='utf-8')
        self.supervisor.start(task_file=attempt.task_file, receipt_file=attempt.receipt_file,
                              token=attempt.token, submission='read task; save result and receipt',
                              command=['codex'] if number == 1 else None)

    def activity(self, count: int, at: float | None = None) -> None:
        atomic_write_json(self.run.root / 'worker-activity.json',
                          {'bytes': count, 'last_output_at': self.now if at is None else at})

    def receipt(self, number: int = 1, *, status: str = 'ready_for_verification', result: bool = True) -> None:
        attempt = self.agent.attempts[number - 1]
        if result:
            attempt.result_file.write_text('Complete report', encoding='utf-8')
        atomic_write_json(attempt.receipt_file, {'schema_version': 1, 'token': attempt.token,
                                               'status': status, 'summary': 'finished'})

    def running(self) -> None:
        self.supervisor.poll()
        self.now += 10
        self.supervisor.poll()
        self.session.send_text.assert_called_once()
        self.session.capture.return_value = '› read task; save result and receipt'
        self.session.activity_status.return_value = '36:0:1::1'
        self.now += 10
        self.assertEqual(self.supervisor.poll()['status'], 'submitting')
        self.session.capture.return_value = '› Ask Codex to do anything'
        self.session.activity_status.return_value = '2:0:1::1'
        self.now += 10
        self.assertEqual(self.supervisor.poll()['status'], 'running')

    def test_silence_persists_across_watch_restarts(self) -> None:
        self.running()
        self.now += 299
        self.assertEqual(self.new_supervisor().watch(wait_seconds=0)['status'], 'running')
        self.now += 1
        self.assertEqual(self.new_supervisor().watch(wait_seconds=0)['reason'], 'silent')
        self.session.send_text.assert_called_once()

    def test_new_output_near_watch_deadline_restarts_only_silence_clock(self) -> None:
        self.running()
        started = self.now

        def advance(seconds: float) -> None:
            self.now += seconds
            if self.now == started + 290:
                self.session.capture.return_value = '检查全部通过，正在保存报告\n› '
                self.session.activity_status.return_value = '2:1:1::1'
                self.activity(100)

        with (mock.patch('conductor.supervision.time.monotonic', side_effect=lambda: self.now),
              mock.patch('conductor.supervision.time.sleep', side_effect=advance)):
            review = self.supervisor.watch(wait_seconds=300)
            self.assertEqual(self.now, started + 300)
            self.assertEqual((review['status'], review['reason'], review['silence_seconds']),
                             ('running', 'review', 10))
            # 下次 watch 沿用上次活动时间；达到最后一次输出后 300 秒才触发。
            silent = self.new_supervisor().watch(wait_seconds=300)
            self.assertEqual(self.now, started + 590)
            self.assertEqual((silent['status'], silent['reason'], silent['silence_seconds']),
                             ('needs_attention', 'silent', 300))
        records = [json.loads(line) for line in self.supervisor.log_file.read_text().splitlines()]
        review_log = next(row for row in records if row.get('reason') == 'review')
        silent_log = next(row for row in records if row.get('reason') == 'silent')
        self.assertEqual(review_log['silence_seconds'], 10)
        self.assertEqual(silent_log['silence_seconds'], 300)
        self.assertEqual(review_log['last_activity_at'], started + 290)
        self.assertEqual(silent_log['worker_idle_timeout_seconds'], 300)
        self.session.close.assert_not_called()
        self.assertEqual(silent['recoveries'], 0)

    def test_working_timer_updates_keep_running_across_multiple_watch_windows(self) -> None:
        self.running()
        started = self.now
        request = json.loads(self.run.request_file.read_text())
        request['sdk_heartbeat_counts_as_activity'] = False
        atomic_write_json(self.run.request_file, request)

        def advance(seconds: float) -> None:
            self.now += seconds
            elapsed = int(self.now - started)
            self.session.capture.return_value = f'• Working ({elapsed}s • esc to interrupt)\n› Ask Codex to do anything'
            self.session.activity_status.return_value = '2:1:1::1'
            self.activity(elapsed)

        with (mock.patch('conductor.supervision.time.monotonic', side_effect=lambda: self.now),
              mock.patch('conductor.supervision.time.sleep', side_effect=advance)):
            for window in (1, 2):
                review = self.new_supervisor().watch(wait_seconds=300)
                self.assertEqual(self.now, started + window * 300)
                self.assertEqual((review['status'], review['reason'], review['silence_seconds']),
                                 ('running', 'review', 0))
        self.now += 299
        self.assertEqual(self.new_supervisor().poll()['status'], 'running')
        self.now += 1
        self.assertEqual(self.new_supervisor().poll()['reason'], 'silent')
        self.session.close.assert_not_called()

    def test_interrupted_submission_is_not_automatically_replayed_by_next_watch(self) -> None:
        self.supervisor.poll()
        self.now += 10
        self.session.send_text.side_effect = TmuxError('transport interrupted after paste')
        with self.assertRaises(TmuxError):
            self.supervisor.poll()
        self.session.send_text.side_effect = None
        self.now += 10
        self.new_supervisor().watch(wait_seconds=0)
        self.session.send_text.assert_called_once()

    def test_all_worker_changes_reset_activity(self) -> None:
        self.running()
        for value in ('spinner ⠙', 'elapsed 55s', 'cursor', 'identical repeated bytes'):
            with self.subTest(value=value):
                self.now += 299
                if value == 'cursor':
                    self.session.activity_status.return_value = '2:3:0::1'
                elif value == 'identical repeated bytes':
                    self.activity(500)
                else:
                    self.session.capture.return_value = value
                result = self.supervisor.poll()
                self.assertEqual(result['status'], 'running')
                self.assertEqual(result['silence_seconds'], 0)
        self.now += 300
        self.assertEqual(self.supervisor.poll()['reason'], 'silent')

    def test_old_bytes_do_not_reset_clock_when_watch_restarts(self) -> None:
        self.running()
        self.activity(200, at=self.now + 1)
        self.session.capture.return_value = 'old output'
        self.now += 301
        self.assertEqual(self.new_supervisor().poll()['reason'], 'silent')

    def test_sdk_heartbeat_counts_by_default_and_can_be_excluded(self) -> None:
        self.running()
        self.now += 301
        atomic_write_json(self.run.root / 'sdk-heartbeat.json', {'last_output_at': self.now})
        result = self.supervisor.watch(wait_seconds=0)
        self.assertEqual(result['status'], 'running')
        self.assertIn('screen', result)
        request = json.loads(self.run.request_file.read_text())
        request['sdk_heartbeat_counts_as_activity'] = False
        atomic_write_json(self.run.request_file, request)
        self.now += 301
        atomic_write_json(self.run.root / 'sdk-heartbeat.json', {'last_output_at': self.now})
        self.assertEqual(self.new_supervisor().poll()['reason'], 'silent')

    def test_repeated_errors_count_even_when_screen_identical(self) -> None:
        self.running()
        self.session.capture.return_value = 'Network error\n› '
        self.session.activity_status.return_value = '2:1:1::1'
        attention = self.supervisor.poll()
        self.assertEqual(attention['reason'], 'worker_error')
        self.supervisor.watch(wait_seconds=0, acknowledge=attention['id'])
        self.now += 299
        self.activity(200)
        result = self.supervisor.poll()
        self.assertEqual(result['status'], 'running')
        self.assertEqual(result['silence_seconds'], 0)
        self.now += 300
        self.assertEqual(self.supervisor.poll()['reason'], 'silent')

    def test_report_or_screen_cannot_replace_receipt(self) -> None:
        self.running()
        self.agent.attempts[0].result_file.write_text('done')
        self.session.capture.return_value = '任务完成，result.md 已写入'
        self.assertEqual(self.supervisor.poll()['status'], 'running')
        self.now += 300
        self.assertEqual(self.supervisor.poll()['reason'], 'silent')
        self.receipt()
        self.assertEqual(self.supervisor.poll()['status'], 'receipt_ready')

    def test_invalid_receipt_is_archived_and_valid_receipt_preempts_recovery(self) -> None:
        self.receipt(result=False)
        result = self.supervisor.poll()
        self.assertEqual(result['reason'], 'invalid_receipt')
        self.supervisor.recover(observation=result['id'], reason='补齐结果')
        self.assertTrue(self.agent.attempts[0].receipt_file.with_name('receipt.invalid-1.json').exists())
        attention = self.supervisor.watch(wait_seconds=0)
        self.receipt()
        before = self.session.send_text.call_count
        with self.assertRaisesRegex(SupervisionError, 'receipt has arrived'):
            self.supervisor.recover(observation=attention['id'], reason='retry')
        self.assertEqual(self.session.send_text.call_count, before)
        self.assertEqual(self.supervisor.poll()['status'], 'receipt_ready')

    def test_receipt_arriving_during_interrupt_is_not_archived_or_overwritten(self) -> None:
        attention = self.supervisor.watch(wait_seconds=0)
        self.session.send_keys.side_effect = lambda key: self.receipt()
        with self.assertRaisesRegex(SupervisionError, 'receipt has arrived'):
            self.supervisor.recover(observation=attention['id'], reason='retry', interrupt=True)
        self.session.send_text.assert_not_called()
        self.assertTrue(self.agent.attempts[0].receipt_file.exists())

    def test_recovery_limit_survives_watch_restarts_and_business_rework(self) -> None:
        for count in range(1, 6):
            self.supervisor = self.new_supervisor()
            observation = self.supervisor.watch(wait_seconds=0)
            result = self.supervisor.recover(observation=observation['id'], reason='continue')
            self.assertEqual(result['recoveries'], count)
            if count == 3:
                self.receipt()
                self.start(2)
        result = self.new_supervisor().watch(wait_seconds=0)
        with self.assertRaisesRegex(SupervisionError, 'recovery limit'):
            self.new_supervisor().recover(observation=result['id'], reason='sixth')
        self.assertEqual(self.session.send_text.call_count, 5)
        self.factory.create.assert_called_once()

    def test_budget_cannot_be_increased_above_five(self) -> None:
        request = json.loads(self.run.request_file.read_text())
        request['max_recovery_attempts'] = 6
        atomic_write_json(self.run.request_file, request)
        with self.assertRaisesRegex(SupervisionError, '1 to 5'):
            self.new_supervisor()

    def test_concurrent_recovery_cannot_reuse_observation_or_exceed_budget(self) -> None:
        observation = self.supervisor.watch(wait_seconds=0)
        with self.supervisor.locked() as state:
            state['recoveries'] = 4
            self.supervisor.save(state)

        def recover(_: int):
            try:
                return self.new_supervisor().recover(observation=observation['id'], reason='continue')['status']
            except SupervisionError:
                return 'rejected'

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(recover, range(2)))
        self.assertCountEqual(results, ['recovering', 'rejected'])
        self.session.send_text.assert_called_once()
        self.assertEqual(self.supervisor.poll()['recoveries'], 5)

    def test_watch_samples_every_ten_seconds_and_returns_at_deadline(self) -> None:
        def advance(seconds: float) -> None:
            self.now += seconds

        with (mock.patch('conductor.supervision.time.monotonic', side_effect=lambda: self.now),
              mock.patch('conductor.supervision.time.sleep', side_effect=advance) as sleep):
            result = self.supervisor.watch(wait_seconds=25)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [10, 10, 5])
        self.assertEqual(result['status'], 'submitting')
        self.assertTrue(Path(result['snapshot_file']).is_file())

    def paste(self) -> None:
        self.supervisor.poll()
        self.now += 10
        self.assertEqual(self.supervisor.poll()['status'], 'submitting')
        self.session.send_text.assert_called_once_with('read task; save result and receipt', submit=False)
        self.session.send_keys.assert_not_called()

    def draft(self, extra_lines: int = 0) -> None:
        self.session.capture.return_value = ('› <dsh_conductor_handoff>\n  任务\n'
                                             '  </dsh_conductor_handoff>\n' + '  \n' * extra_lines)
        self.session.activity_status.return_value = f'2:{2 + extra_lines}:1::1'

    def test_newline_instead_of_submit_is_reported_despite_heartbeat(self) -> None:
        self.paste()
        self.draft()
        self.now += 10
        self.assertEqual(self.supervisor.poll()['status'], 'submitting')
        self.session.send_keys.assert_called_once_with('Enter')
        self.draft(extra_lines=1)  # 第一次 Enter 被 TUI 吸收为换行。
        self.now += 10
        atomic_write_json(self.run.root / 'sdk-heartbeat.json', {'last_output_at': self.now})
        result = self.new_supervisor().watch(wait_seconds=300)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(result['reason'], 'submission_pending')
        self.assertEqual(result['silence_seconds'], 0)
        self.assertEqual(result['phase'], 'submitting')
        with self.assertRaisesRegex(SupervisionError, 'use choose'):
            self.supervisor.recover(observation=result['id'], reason='继续')
        # 只补一个 Enter，不重复粘贴正文；看到空输入框后才允许标记 running。
        def accept(key: str) -> None:
            self.session.capture.return_value += '\n• Working (1s • esc to interrupt)\n› Ask Codex to do anything'
            self.session.activity_status.return_value = '2:6:1::1'
        self.session.send_keys.side_effect = accept
        result = self.supervisor.choose(observation=result['id'], keys=['Enter'], reason='提交尚未发送的任务')
        self.assertEqual(result['status'], 'running')
        self.assertEqual(result['recoveries'], 1)
        self.session.send_text.assert_called_once()

    def test_unconfirmed_submission_has_independent_persisted_deadline(self) -> None:
        self.paste()
        for elapsed in (10, 20, 30):
            self.now = 1010 + elapsed
            atomic_write_json(self.run.root / 'sdk-heartbeat.json', {'last_output_at': self.now})
            self.activity(elapsed)
            result = self.new_supervisor().poll()
            self.assertEqual(result['silence_seconds'], 0)
            self.assertEqual(result['status'], 'needs_attention' if elapsed == 30 else 'submitting')
        self.assertEqual(result['reason'], 'submission_unconfirmed')
        self.session.send_keys.assert_not_called()
        self.session.send_text.assert_called_once()

    def test_interruption_after_enter_does_not_replay_paste_or_enter(self) -> None:
        self.paste()
        self.draft()
        self.now += 10
        self.session.send_keys.side_effect = TmuxError('interrupted after Enter')
        with self.assertRaises(TmuxError):
            self.supervisor.poll()
        self.session.send_keys.side_effect = None
        self.now += 10
        self.assertEqual(self.new_supervisor().poll()['reason'], 'submission_pending')
        self.session.send_keys.assert_called_once_with('Enter')
        self.session.send_text.assert_called_once()

    def test_repeated_submit_keys_share_budget_even_when_draft_changes(self) -> None:
        self.paste()
        self.draft()
        self.now += 10
        self.supervisor.poll()
        for count in range(1, 7):
            self.now += 10
            self.draft(extra_lines=count)
            result = self.new_supervisor().poll()
            self.assertEqual(result['reason'], 'submission_pending')
            if count <= 5:
                result = self.supervisor.choose(observation=result['id'], keys=['Enter'], reason='补交')
                self.assertEqual(result['recoveries'], count)
            else:
                with self.assertRaisesRegex(SupervisionError, 'recovery limit'):
                    self.supervisor.choose(observation=result['id'], keys=['Enter'], reason='第六次')
        self.assertEqual(self.session.send_keys.call_count, 6)  # 首次提交 + 五次恢复。
        self.session.send_text.assert_called_once()

    def test_recovery_and_business_rework_also_require_submission_confirmation(self) -> None:
        self.running()
        observation = self.supervisor.watch(wait_seconds=0)
        self.supervisor.recover(observation=observation['id'], reason='补齐报告')
        self.assertFalse(self.session.send_text.call_args.kwargs['submit'])
        self.assertEqual(self.supervisor.poll()['status'], 'submitting')
        self.receipt()
        self.assertEqual(self.supervisor.poll()['status'], 'receipt_ready')
        self.start(2)
        self.supervisor.poll()
        self.now += 10
        self.assertEqual(self.supervisor.poll()['status'], 'submitting')
        self.assertFalse(self.session.send_text.call_args.kwargs['submit'])

    def test_pending_submission_cannot_send_multiple_enters_at_once(self) -> None:
        self.draft()
        result = self.supervisor.poll()
        with self.assertRaisesRegex(SupervisionError, 'exactly one Enter'):
            self.supervisor.choose(observation=result['id'], keys=['Enter', 'Enter'], reason='submit')
        self.session.send_keys.assert_not_called()

    def test_acknowledging_silence_defers_reminder_without_resetting_clock(self) -> None:
        self.running()
        self.now += 300
        observation = self.supervisor.poll()
        start = self.now
        with (mock.patch('conductor.supervision.time.monotonic', side_effect=lambda: self.now),
              mock.patch('conductor.supervision.time.sleep', side_effect=lambda seconds: setattr(self, 'now', self.now + seconds))):
            result = self.supervisor.watch(wait_seconds=20, acknowledge=observation['id'])
        self.assertEqual(self.now, start + 20)
        self.assertEqual(result['reason'], 'silent')
        self.assertEqual(result['silence_seconds'], 320)

    def test_dsh_chooses_menus_and_repeated_choices_consume_recovery_budget(self) -> None:
        self.session.capture.return_value = '› 1. Continue\nPress enter to continue\n10s'
        attention = self.supervisor.poll()
        self.session.send_keys.assert_not_called()
        self.supervisor.choose(observation=attention['id'], keys=['Enter'], reason='选择继续')
        self.assertEqual(self.supervisor.poll()['recoveries'], 0)
        self.session.capture.return_value = '  1. Continue\nPress enter to continue\n20s'
        attention = self.supervisor.poll()
        result = self.supervisor.choose(observation=attention['id'], keys=['Enter'], reason='重试卡住菜单')
        self.assertEqual(result['recoveries'], 1)
        self.session.send_text.assert_not_called()

    def test_stale_menu_is_rejected_and_ready_submission_is_sent_once(self) -> None:
        self.session.capture.return_value = '› 1. Continue\nPress enter to continue'
        result = self.supervisor.poll()
        self.session.capture.return_value = '› Ask Codex to do anything'
        with self.assertRaisesRegex(SupervisionError, 'menu changed'):
            self.supervisor.choose(observation=result['id'], keys=['Enter'], reason='yes')
        self.running()
        self.now += 20
        self.supervisor.poll()
        self.session.send_text.assert_called_once()

    def test_menu_navigation_does_not_charge_first_confirmation(self) -> None:
        self.session.capture.return_value = '› 1. Continue\n  2. Cancel\nPress enter to continue'
        observation = self.supervisor.poll()
        moved = self.supervisor.choose(observation=observation['id'], keys=['Down'], reason='查看选项')
        confirmed = self.supervisor.choose(observation=moved['id'], keys=['Enter'], reason='确认选择')
        self.assertEqual(confirmed['recoveries'], 0)
        retried = self.supervisor.choose(observation=confirmed['id'], keys=['Enter'], reason='菜单仍未响应')
        self.assertEqual(retried['recoveries'], 1)

    def test_recover_cannot_append_to_pending_paste(self) -> None:
        self.session.capture.return_value = '› [Pasted Content 100 chars]'
        self.session.activity_status.return_value = '28:0:1::1'
        observation = self.supervisor.poll()
        with self.assertRaisesRegex(SupervisionError, 'use choose'):
            self.supervisor.recover(observation=observation['id'], reason='继续')
        self.session.send_text.assert_not_called()

    def test_stop_without_receipt_preserves_final_history_and_prevents_further_actions(self) -> None:
        result = self.supervisor.stop(reason='worker unrecoverable')
        self.assertEqual(result['status'], 'stopped')
        self.session.capture.assert_called_with(history_lines=HISTORY_LIMIT)
        self.session.close.assert_called_once()
        self.assertEqual(self.new_supervisor().poll()['status'], 'stopped')
        self.assertFalse(self.agent.attempts[0].receipt_file.exists())
        with self.assertRaisesRegex(SupervisionError, 'already stopped'):
            self.start(2)

    def test_stop_before_submission_and_invalid_wait(self) -> None:
        self.supervisor.path.unlink()
        result = self.supervisor.stop(reason='startup failed')
        self.assertEqual(result['attempt'], 0)
        for value in (float('nan'), float('inf'), -1, 301):
            with self.subTest(value=value), self.assertRaises(SupervisionError):
                self.supervisor.watch(wait_seconds=value)

    def test_stop_still_closes_worker_if_final_capture_fails(self) -> None:
        self.session.capture.side_effect = TmuxError('capture failed')
        result = self.supervisor.stop(reason='unrecoverable')
        self.assertEqual(result['status'], 'stopped')
        self.session.close.assert_called_once()
        records = [json.loads(line) for line in self.supervisor.log_file.read_text().splitlines()]
        self.assertEqual(records[-1]['capture_error'], 'capture failed')

    def test_stale_report_and_wrong_run_identity_are_rejected(self) -> None:
        self.supervisor.path.unlink()
        self.agent.attempts[0].result_file.write_text('stale')
        with self.assertRaisesRegex(SupervisionError, 'stale receipt or result'):
            self.start(1)
        with self.assertRaisesRegex(SupervisionError, 'session does not match'):
            Supervisor(self.run.request_file, CODEX, 'unrelated')

    def test_global_stop_marker_blocks_controller_after_sdk_exit(self) -> None:
        atomic_write_json(self.run.root / 'sdk-stop.json', {'run_id': self.run.run_id})
        self.assertEqual(self.supervisor.poll()['status'], 'stopped')
        with self.assertRaisesRegex(SupervisionError, 'already stopped'):
            self.start(2)

    def test_dead_worker_and_broken_monitor_are_reported(self) -> None:
        self.session.status.return_value.pane_dead = True
        self.assertEqual(self.supervisor.poll()['reason'], 'worker_exited')
        self.session.status.return_value.pane_dead = False
        self.session.activity_status.return_value = '0:1:1::0'
        self.assertEqual(self.supervisor.poll()['reason'], 'monitor_error')

    def test_non_utf8_receipt_is_reported_for_dsh_recovery(self) -> None:
        self.agent.attempts[0].receipt_file.write_bytes(b'\xff')
        observation = self.supervisor.poll()
        self.assertEqual(observation['reason'], 'invalid_receipt')
        self.assertIn('invalid JSON', observation['detail'])


if __name__ == '__main__':
    unittest.main()
