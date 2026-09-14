from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from conductor.cli import main


class CliTests(unittest.TestCase):
    def test_install_skills_targets_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["install-skills", "--workspace", str(workspace)])
            output = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(output["skill_dir"], str(workspace / ".dsh" / "skills"))
            self.assertTrue((workspace / ".dsh" / "skills" / "tmux-claude-code" / "SKILL.md").is_file())
            self.assertTrue((workspace / ".dsh" / "skills" / "tmux-codex" / "SKILL.md").is_file())

    def test_run_keeps_stdout_as_one_json_object(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            home = root / "dsh-home"
            home.mkdir()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.dict("os.environ", {"FAKE_DSH_SDK": "1"}),
                mock.patch("conductor.skills.shutil.which", return_value="/bin/true"),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = main(
                    [
                        "run",
                        "--workspace",
                        str(workspace),
                        "--prompt",
                        "创建 fixture.txt。验收标准：文件存在。",
                        "--dsh-bin",
                        str(fixture),
                        "--dsh-home",
                        str(home),
                        "--state-dir",
                        str(root / "state"),
                        "--no-worker-log",
                    ]
                )
            output = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(output["status"], "accepted")
            self.assertEqual(output["plan"]["agent"], "claude")
            self.assertIn("conductor run", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
