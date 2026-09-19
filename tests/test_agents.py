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
    def test_codex_distinguishes_composer_from_identical_history(self) -> None:
        submitted = '› <dsh_conductor_handoff>\n  任务\n  </dsh_conductor_handoff>\n'
        for screen, cursor, expected in (
            (submitted, '26:2:1::1', 'pending'),
            (submitted, '2:0:1::1', 'pending'),
            (submitted + '  \n', '2:3:1::1', 'pending'),
            ('› [Pasted Content 5000 chars]', '29:0:1::1', 'pending'),
            (submitted + '\n• Working (1s • esc to interrupt)\n› Ask Codex to do anything', '2:5:1::1', 'empty'),
            ('› [Pasted Content 5000 chars]\n• 完成\n› ', '2:2:1::1', 'empty'),
            ('› Ask Codex to do anything', '2:0:1::1', 'empty'),
            ('  长草稿顶部已滚出屏幕\n  </dsh_conductor_handoff>', '26:1:1::1', 'unknown'),
            ('› draft', '0:0:0::1', 'unknown'),
            ('› draft', 'invalid', 'unknown'),
        ):
            with self.subTest(screen=screen, cursor=cursor):
                self.assertEqual(CODEX.submission_status(screen, cursor), expected)
                self.assertEqual(CODEX.has_pending_submission(screen, cursor), expected == 'pending')

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
