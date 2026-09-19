from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from conductor.agents.base import main, _submission_prompt
from conductor.agents.claude import CLAUDE
from conductor.agents.codex import CODEX
from conductor.models import WorkerReceipt


class AgentControllerTests(unittest.TestCase):
    def test_both_agents_require_result_before_writing_receipt(self) -> None:
        for adapter in (CLAUDE, CODEX):
            for status in ("ready_for_verification", "blocked"):
                with self.subTest(agent=adapter.kind, status=status), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    receipt = root / "receipt.json"
                    result = root / "result.md"
                    args = ["complete", "--receipt", str(receipt), "--token", "token-a",
                            "--status", status, "--summary", "完整结果已落盘"]
                    for invalid in (None, " \n"):
                        if invalid is not None:
                            result.write_text(invalid, encoding="utf-8")
                        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                            self.assertEqual(main(adapter, args), 1)
                        self.assertFalse(receipt.exists())
                    full_text = "\n".join(f"结果第 {i} 行" for i in range(10000))
                    result.write_text(full_text, encoding="utf-8")
                    output = io.StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(main(adapter, args), 0)
                    self.assertEqual(json.loads(output.getvalue())["result_file"], str(result))
                    self.assertEqual(WorkerReceipt.load(receipt, expected_token="token-a").status, status)
                    self.assertEqual(result.read_text(encoding="utf-8"), full_text)

    def test_submission_keeps_task_body_in_file(self) -> None:
        prompt = _submission_prompt(task_file=Path("/tmp/task.md"), script=Path("/skill/controller.py"),
                                    receipt=Path("/tmp/receipt.json"), token="token-a")
        self.assertIn("/tmp/task.md", prompt)
        self.assertIn("/tmp/result.md", prompt)
        self.assertIn(" complete ", prompt)


if __name__ == "__main__":
    unittest.main()
