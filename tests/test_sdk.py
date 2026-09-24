from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path
import json

from conductor.progress import RunEvent
from conductor.sdk import Conductor, ConductorConfig, ConductorError


class SdkTests(unittest.TestCase):
    def test_report_opt_in_is_a_stable_inline_snapshot_for_both_agents(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as directory:
                config = ConductorConfig(dsh_bin=str(fixture), worker_log=False, include_report=True,
                                         timeout_seconds=10, dsh_extra_env={"FAKE_DSH_SDK": "1", "FAKE_DSH_AGENT": agent})
                with mock.patch("conductor.skills.shutil.which", return_value="/bin/true"):
                    result = Conductor(directory, config).run("完整任务报告")
                payload = result.to_json()
                self.assertIn("完整检查结果", payload["report"])
                agent_name = {"claude": "Claude Code", "codex": "Codex"}[agent]
                self.assertIn(f"### {agent_name} 第 1 轮报告", payload["report"])
                self.assertIn("## 二、任务验收报告", payload["report"])
                self.assertIn("最终结论：通过（accepted）", payload["report"])
                self.assertIn("fixture accepted", payload["report"])
                self.assertNotIn("criterion-1", payload["report"])
                self.assertNotIn("test fixture", payload["report"])
                self.assertNotIn("fixture.txt exists", payload["report"])
                self.assertNotIn("### 产物", payload["report"])
                self.assertEqual(payload["verdict"]["schema_version"], 2)
                self.assertEqual(payload["verdict"]["checks"], [{
                    "criterion_id": "criterion-1", "criterion": "fixture.txt exists",
                    "method": "test fixture", "evidence": "fixture.txt exists", "passed": True,
                }])
                self.assertEqual(payload["verdict"]["artifacts"], ["fixture.txt"])
                self.assertEqual(payload["dsh"]["final_text"], "fixture complete")
                self.assertEqual(payload["worker_result"], str(result.worker_result))
                self.assertEqual(Path(payload["report_file"]).read_text(encoding="utf-8"), result.report)
                self.assertNotIn("report_warnings", payload)
                self.assertTrue(json.loads((result.state_directory / "request.json").read_text())["include_report"])
                result.worker_result.write_text("后续文件变化不影响已返回的正文")
                result.report_file.unlink()
                self.assertEqual(result.to_json(), payload)

    def test_report_missing_worker_does_not_turn_rejection_into_execution_error(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        with tempfile.TemporaryDirectory() as directory:
            config = ConductorConfig(dsh_bin=str(fixture), worker_log=False, include_report=True,
                                     timeout_seconds=10, dsh_extra_env={"FAKE_DSH_SDK": "1", "FAKE_DSH_REJECTED": "1"})
            with mock.patch("conductor.skills.shutil.which", return_value="/bin/true"):
                result = Conductor(directory, config).run("拒绝任务")
            self.assertFalse(result.accepted)
            self.assertIn("未通过（rejected）", result.report)
            self.assertTrue(result.report_warnings)

    def test_report_timeout_preserves_error_and_never_accepts_unvalidated_verdict(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        with tempfile.TemporaryDirectory() as directory:
            config = ConductorConfig(dsh_bin=str(fixture), worker_log=False, include_report=True,
                                     timeout_seconds=2, dsh_extra_env={"FAKE_DSH_SDK": "1", "FAKE_DSH_NO_TURN": "1"})
            with mock.patch("conductor.skills.shutil.which", return_value="/bin/true"):
                with self.assertRaises(ConductorError) as caught:
                    Conductor(directory, config).run("超时任务")
            error = caught.exception
            self.assertEqual(error.code, "timeout")
            self.assertIsNone(error.result)
            self.assertIn("未形成可返回的有效验收结论", error.report)
            self.assertNotIn("最终结论：通过", error.report)
            self.assertEqual(error.to_json()["status"], "error")

    def test_include_report_requires_boolean(self) -> None:
        with self.assertRaisesRegex(ValueError, "include_report"):
            ConductorConfig(include_report="true")

    def test_rejected_without_receipt_stops_worker_even_with_keep_session_and_no_logs(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        from conductor.tmux import TmuxSession
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = ConductorConfig(dsh_bin=str(fixture), worker_log=False, keep_session=True,
                                     timeout_seconds=10, dsh_extra_env={"FAKE_DSH_SDK": "1", "FAKE_DSH_REJECTED": "1", "FAKE_DSH_WORKER": "1"})
            with mock.patch("conductor.skills.shutil.which", return_value="/bin/true"):
                result = Conductor(workspace, config).run("test")
            self.assertFalse(result.accepted)
            self.assertIsNone(result.worker_result)
            self.assertEqual(result.cleanup.status, "completed")
            self.assertTrue((result.state_directory / "sdk-stop.json").exists())
            self.assertIn("fixture worker evidence", (result.state_directory / "sdk-stop-claude.txt").read_text())
            self.assertFalse(TmuxSession(result.session, socket_path=result.tmux_socket).exists())

    def test_overall_timeout_stops_worker_and_retains_evidence(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        from conductor.tmux import TmuxSession
        import json
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = ConductorConfig(dsh_bin=str(fixture), keep_session=True, worker_log=False, timeout_seconds=3,
                                     dsh_extra_env={"FAKE_DSH_SDK": "1", "FAKE_DSH_WORKER": "1", "FAKE_DSH_NO_TURN": "1"})
            with mock.patch("conductor.skills.shutil.which", return_value="/bin/true"):
                with self.assertRaises(ConductorError) as caught:
                    Conductor(workspace, config).run("test")
            error = caught.exception
            self.assertEqual(error.code, "timeout")
            # 3 秒总预算只预留 0.3 秒；CI 繁忙时可以如实报告核验未完成，
            # 但 worker 必须已停止，且剩余核验可通过恢复清理完成。
            self.assertIn(error.cleanup.status, {"completed", "incomplete"})
            runtime = json.loads((error.state_directory / "runtime.json").read_text())
            for name in runtime["sessions"]:
                self.assertFalse(TmuxSession(name, socket_path=Path(runtime["tmux_socket"])).exists())
            from conductor import cleanup_run
            self.assertEqual(cleanup_run(error.state_directory).status, "completed")

    def test_final_sampling_precedes_worker_cleanup_and_retention_is_reported(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        from conductor import cleanup_run
        from conductor.tmux import TmuxSession
        for keep_session in (False, True):
            with self.subTest(keep_session=keep_session), tempfile.TemporaryDirectory() as directory:
                config = ConductorConfig(dsh_bin=str(fixture), keep_session=keep_session, timeout_seconds=10,
                                         dsh_extra_env={"FAKE_DSH_SDK": "1", "FAKE_DSH_WORKER": "1"})
                with mock.patch("conductor.skills.shutil.which", return_value="/bin/true"):
                    result = Conductor(directory, config).run("test")
                try:
                    self.assertEqual(result.cleanup.status, "retained" if keep_session else "completed")
                    self.assertIn("fixture worker evidence", result.worker_log.read_text())
                    self.assertEqual(TmuxSession(result.session, socket_path=result.tmux_socket).exists(), keep_session)
                finally:
                    self.assertEqual(cleanup_run(result.state_directory).status, "completed")

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
            self.assertIsNone(result.report)
            self.assertTrue({"report", "report_file", "report_warnings"}.isdisjoint(result.to_json()))
            self.assertNotIn("include_report", json.loads((result.state_directory / "request.json").read_text()))
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
