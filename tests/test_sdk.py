from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from conductor.progress import RunEvent
from conductor.sdk import Conductor, ConductorConfig, ConductorError
from conductor.tmux import HISTORY_LIMIT


class SdkTests(unittest.TestCase):
    def test_rejected_without_receipt_stops_worker_even_with_keep_session_and_no_logs(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            sessions = {}

            def session_factory(name: str) -> mock.Mock:
                session = sessions.setdefault(name, mock.Mock())
                session.exists.return_value = 'claude' in name
                session.status.return_value = SimpleNamespace(agent='claude', workspace=str(workspace))
                session.capture.return_value = 'last long output'
                return session

            config = ConductorConfig(dsh_bin=str(fixture), dsh_home=workspace, worker_log=False, keep_session=True,
                                     dsh_extra_env={"FAKE_DSH_SDK": "1", "FAKE_DSH_REJECTED": "1"})
            with (mock.patch("conductor.skills.shutil.which", return_value="/bin/true"),
                  mock.patch("conductor.sdk.TmuxSession", side_effect=session_factory)):
                result = Conductor(workspace, config).run('test')
            self.assertFalse(result.accepted)
            self.assertIsNone(result.worker_result)
            self.assertTrue((result.state_directory / 'sdk-stop.json').exists())
            self.assertEqual((result.state_directory / 'sdk-stop-claude.txt').read_text(), 'last long output')
            sessions[result.session].capture.assert_called_once_with(history_lines=HISTORY_LIMIT)
            sessions[result.session].close.assert_called_once()

    def test_overall_timeout_stops_only_matching_candidate_after_final_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            order = []
            sessions = {}

            def session_factory(name: str) -> mock.Mock:
                session = sessions.setdefault(name, mock.Mock())
                session.exists.return_value = True
                # 另一个候选即使存在，也不能误杀不同身份的会话。
                session.status.return_value = SimpleNamespace(agent='codex', workspace=str(workspace))
                session.capture.side_effect = lambda **kw: order.append('capture') or 'timeout evidence'
                session.close.side_effect = lambda: order.append('close')
                return session

            config = ConductorConfig(dsh_home=workspace, keep_session=True, worker_log=False)
            with (mock.patch("conductor.skills.shutil.which", return_value="/bin/true"),
                  mock.patch("conductor.sdk.TmuxSession", side_effect=session_factory),
                  mock.patch("conductor.sdk.DshClient") as client):
                client.return_value.__enter__.return_value.run.return_value = SimpleNamespace(status='timeout', stderr_tail='')
                with self.assertRaisesRegex(ConductorError, 'timeout') as caught:
                    Conductor(workspace, config).run('test')
            self.assertEqual(order, ['capture', 'close'])
            root = caught.exception.state_directory
            self.assertTrue((root / 'sdk-stop.json').exists())
            self.assertEqual((root / 'sdk-stop-codex.txt').read_text(), 'timeout evidence')
            for name, session in sessions.items():
                if 'claude' in name:
                    session.close.assert_not_called()

    def test_session_cleanup_follows_final_sampling_and_respects_identity(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        for keep_session, matching in ((False, True), (True, True), (False, False)):
            with self.subTest(keep_session=keep_session, matching=matching), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                order: list[str] = []

                def follower(**kwargs: object) -> mock.Mock:
                    instance = mock.Mock()
                    instance.start.return_value = instance
                    instance.stop.side_effect = lambda: order.append(f"stop {kwargs['agent']}")
                    return instance

                config = ConductorConfig(
                    dsh_bin=str(fixture), dsh_home=workspace, keep_session=keep_session,
                    dsh_extra_env={"FAKE_DSH_SDK": "1"}, timeout_seconds=2,
                )
                with (
                    mock.patch("conductor.skills.shutil.which", return_value="/bin/true"),
                    mock.patch("conductor.sdk.WorkerLogFollower", side_effect=follower),
                    mock.patch("conductor.sdk.TmuxSession") as session_type,
                ):
                    session = session_type.return_value
                    session.exists.return_value = True
                    session.status.return_value = SimpleNamespace(
                        agent="claude" if matching else "codex", workspace=str(workspace),
                    )
                    session.close.side_effect = lambda: order.append("close")
                    result = Conductor(workspace, config).run("创建 fixture.txt 并验证文件存在")
                    self.assertTrue(result.accepted)
                    if not keep_session and matching:
                        self.assertEqual(order, ["stop claude", "stop codex", "close"])
                        session_type.assert_called_once_with(result.session)
                    else:
                        self.assertEqual(order, ["stop claude", "stop codex"])
                        session.close.assert_not_called()

    def test_run_returns_plan_verdict_and_events(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            dsh_home = Path(directory) / "dsh-home"
            dsh_home.mkdir()
            # fixture 直接写协议文件，不执行真正 worker。
            config = ConductorConfig(
                dsh_bin=str(fixture),
                dsh_home=dsh_home,
                state_dir=Path(directory) / "state",
                dsh_extra_env={"FAKE_DSH_SDK": "1"},
                worker_log=False,
                timeout_seconds=2,
            )
            events: list[RunEvent] = []
            with mock.patch("conductor.skills.shutil.which", return_value="/bin/true"):
                result = Conductor(workspace, config).run(
                    "请创建 fixture.txt，并确认文件存在。",
                    events.append,
                )
            self.assertTrue(result.accepted)
            self.assertIsNotNone(result.worker_result)
            assert result.worker_result is not None
            self.assertIn("完整检查结果", result.worker_result.read_text(encoding="utf-8"))
            self.assertEqual(result.to_json()["worker_result"], str(result.worker_result))
            self.assertEqual(result.plan.agent.value, "claude")
            self.assertEqual(result.verdict.status, "accepted")
            self.assertTrue(any(event.kind == "turn_end" for event in events))
            self.assertEqual(events[-1].kind, "run_end")
            self.assertTrue((workspace / "fixture.txt").exists())
            self.assertTrue((workspace / ".dsh" / "skills" / "tmux-claude-code" / "SKILL.md").is_file())
            self.assertTrue((workspace / ".dsh" / "skills" / "tmux-codex" / "SKILL.md").is_file())

    def test_sdk_rejects_acceptance_without_result_file_for_either_agent(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                config = ConductorConfig(
                    dsh_bin=str(fixture), dsh_home=workspace,
                    dsh_extra_env={"FAKE_DSH_SDK": "1", "FAKE_DSH_MISSING_RESULT": "1", "FAKE_DSH_AGENT": agent},
                    worker_log=False, timeout_seconds=2,
                )
                with mock.patch("conductor.skills.shutil.which", return_value="/bin/true"):
                    with self.assertRaisesRegex(ConductorError, "cannot read worker result"):
                        Conductor(workspace, config).run("创建 fixture.txt 并验证文件存在")


if __name__ == "__main__":
    unittest.main()
