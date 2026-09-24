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
    def test_report_is_inline_in_single_cli_json(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with (mock.patch.dict("os.environ", {"FAKE_DSH_SDK": "1"}),
                  mock.patch("conductor.skills.shutil.which", return_value="/bin/true"),
                  redirect_stdout(output), redirect_stderr(io.StringIO())):
                code = main(["run", "--workspace", directory, "--prompt", "生成完整报告",
                             "--include-report", "--dsh-bin", str(fixture), "--no-worker-log"])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertIn("完整检查结果", payload["report"])
            self.assertIn("## 二、任务验收报告", payload["report"])
            self.assertIn("最终结论：通过（accepted）", payload["report"])
            self.assertNotIn("test fixture", payload["report"])
            self.assertEqual(payload["verdict"]["checks"][0]["method"], "test fixture")
            self.assertEqual(payload["schema_version"], 1)

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
                        "--no-worker-log",
                    ]
                )
            output = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(output["status"], "accepted")
            self.assertNotIn("report", output)
            self.assertEqual(output["plan"]["agent"], "claude")
            self.assertEqual(
                output["state_directory"].split("/runs/")[0],
                str(workspace / ".dsh-conductor"),
            )
            self.assertIn("conductor run", stderr.getvalue())

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["show", "--workspace", str(workspace)])
            shown = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(shown["run_directory"], output["state_directory"])


if __name__ == "__main__":
    unittest.main()
