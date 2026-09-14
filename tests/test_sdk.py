from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

from conductor.progress import RunEvent
from conductor.sdk import Conductor, ConductorConfig


class SdkTests(unittest.TestCase):
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
            self.assertEqual(result.plan.agent.value, "claude")
            self.assertEqual(result.verdict.status, "accepted")
            self.assertTrue(any(event.kind == "turn_end" for event in events))
            self.assertEqual(events[-1].kind, "run_end")
            self.assertTrue((workspace / "fixture.txt").exists())
            self.assertTrue((workspace / ".dsh" / "skills" / "tmux-claude-code" / "SKILL.md").is_file())
            self.assertTrue((workspace / ".dsh" / "skills" / "tmux-codex" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
