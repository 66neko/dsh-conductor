from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from conductor.agents.base import build_parser, main, _submission_prompt
from conductor.agents.claude import CLAUDE
from conductor.agents.codex import CODEX
from conductor.models import WorkerReceipt


class AgentControllerTests(unittest.TestCase):
    def test_codex_distinguishes_active_reconnect_from_exhausted_and_historical_errors(self) -> None:
        for screen, expected in (
            ('Network error\n• Reconnecting... 1/5 (2s • esc to interrupt)\n› ', 'reconnecting'),
            ('\n\n• Reconnecting... 5/5 (10s • esc to interrupt)\n'
             '  stream disconnected before completion: request timed out\n› ', 'reconnecting'),
            ('Reconnecting… 2/5\nconnection failed\n› ', 'reconnecting'),
            ('• Reconnecting to server…\n› ', 'reconnecting'),
            ('■ exceeded retry limit, last status: 503\n› ', 'retry_exhausted'),
            ('■ stream disconnected before completion: error sending request\n› ', 'retry_exhausted'),
            ('• Working (1s • esc to interrupt)\nReconnecting... 5/5\n'
             '■ stream disconnected before completion\n› ', 'retry_exhausted'),
            ('Network error\nReconnecting... 1/5\n• Working (5s • esc to interrupt)\n› ', None),
            ('■ stream disconnected before completion\n› 继续原任务\n'
             '• Working (5s • esc to interrupt)\n› ', None),
            ('Reconnecting... 1/5\n› 继续原任务\n任务完成\n› ', None),
            ('已检查 reconnect 的兼容\n› ', None),
            ('Network error\n› ', None),
        ):
            with self.subTest(screen=screen):
                self.assertEqual(CODEX.reconnection_status(screen), expected)
                self.assertIsNone(CLAUDE.reconnection_status(screen))

    def test_only_codex_exposes_reconnect_command(self) -> None:
        args = ['reconnect', '--request', '/tmp/request.json', '--session', 'worker',
                '--observation', '3', '--reason', '自动重试耗尽']
        self.assertEqual(build_parser(CODEX).parse_args(args).command, 'reconnect')
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser(CLAUDE).parse_args(args)

    def test_codex_reconnect_status_is_independent_of_configured_retry_count(self) -> None:
        for total in (1, 3, 5, 8, 20, 99):
            for current in sorted({1, total}):
                for ellipsis in ('...', '…'):
                    with self.subTest(current=current, total=total, ellipsis=ellipsis):
                        status_line = f'• Reconnecting{ellipsis} {current}/{total}'
                        active = status_line + ' (10s • esc to interrupt)\n'
                        error = '■ stream disconnected before completion: request timed out\n'
                        self.assertEqual(CODEX.reconnection_status(active + error + '› '), 'reconnecting')
                        # 只有终态错误才要求手动重连，计数本身不能证明已耗尽。
                        self.assertEqual(CODEX.reconnection_status(status_line + '\n' + error + '› '),
                                         'retry_exhausted')
                        self.assertIsNone(CODEX.reconnection_status(status_line + '\n'
                                          '• Working (1s • esc to interrupt)\n› '))

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
        # 提示词引用任务文件和结果文件，但不规定具体格式
        self.assertIn("任务文件", prompt)
        self.assertIn("结果文件", prompt)
        self.assertIn("按任务要求组织", prompt)
        self.assertNotIn("完整回答", prompt)
        self.assertNotIn("subtask-reports.json", prompt)
        # include_report 参数已废弃，不再生成子任务报告
        full = _submission_prompt(task_file=Path("/tmp/task.md"), script=Path("/skill/controller.py"),
                                  receipt=Path("/tmp/receipt.json"), token="token-a", include_report=True)
        self.assertNotIn("subtask-reports.json", full)


if __name__ == "__main__":
    unittest.main()
