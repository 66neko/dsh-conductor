from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from conductor.models import AgentKind, ExecutionPlan, RecordError, Verdict, WorkerReceipt


class RecordTests(unittest.TestCase):
    @staticmethod
    def _plan(workspace: Path, agent: str = "codex") -> ExecutionPlan:
        path = workspace / "plan.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "agent": agent,
                    "agent_reason": "用户指定",
                    "task_summary": "创建产物",
                }
            ),
            encoding="utf-8",
        )
        return ExecutionPlan.load(path, expected_run_id="run-1")

    @staticmethod
    def _receipt(workspace: Path, token: str, status: str = "ready_for_verification") -> Path:
        path = workspace / "receipt.json"
        (workspace / "result.md").write_text("完整任务结果和检查记录", encoding="utf-8")
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "token": token,
                    "status": status,
                    "summary": "worker finished",
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _verdict(workspace: Path, **fields: object) -> Path:
        path = workspace / "verdict.json"
        record = {"schema_version": 3, "status": "accepted", "summary": "报告覆盖全部目标", "remaining_issues": []}
        record.update(fields)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_verdict_only_needs_status_summary_and_issues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            plan = self._plan(workspace)
            receipt = self._receipt(workspace, "token-a")
            verdict = Verdict.load(self._verdict(workspace), plan=plan,
                                   expected_receipts=((receipt, "token-a"), (workspace / "r2.json", "token-b")))
            # run id、agent 与轮次由 SDK 推导，不来自 DSH。
            self.assertEqual((verdict.status, verdict.run_id, verdict.agent, verdict.attempts),
                             ("accepted", "run-1", AgentKind.CODEX, 1))
            self.assertNotIn("checks", verdict.to_json())

    def test_verdict_rejects_old_schema_and_inconsistent_issues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            plan = self._plan(workspace)
            receipt = self._receipt(workspace, "token-a")
            cases = (
                ({"schema_version": 2}, "schema_version"),
                ({"status": "maybe"}, "accepted or rejected"),
                ({"summary": " "}, "summary"),
                ({"remaining_issues": ["缺少说明"]}, "cannot contain"),
                ({"status": "rejected"}, "at least one remaining issue"),
            )
            for fields, message in cases:
                with self.subTest(fields=fields), self.assertRaisesRegex(RecordError, message):
                    Verdict.load(self._verdict(workspace, **fields), plan=plan,
                                 expected_receipts=((receipt, "token-a"),))

    def test_rejected_verdict_needs_no_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            verdict = Verdict.load(self._verdict(workspace, status="rejected", remaining_issues=["agent 不可用"]),
                                   plan=self._plan(workspace), expected_receipts=((workspace / "none.json", "t"),))
            self.assertEqual(verdict.attempts, 0)

    def test_accepted_verdict_requires_bound_final_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            plan = self._plan(workspace)
            with self.assertRaisesRegex(RecordError, "requires a worker receipt"):
                Verdict.load(self._verdict(workspace), plan=plan, expected_receipts=((workspace / "none.json", "t"),))
            receipt = self._receipt(workspace, "token-a", status="blocked")
            with self.assertRaisesRegex(RecordError, "ready_for_verification"):
                Verdict.load(self._verdict(workspace), plan=plan, expected_receipts=((receipt, "token-a"),))
            # 已登记但尚未回执的轮次不能被 accepted 跳过。
            self._receipt(workspace, "token-a")
            with self.assertRaisesRegex(RecordError, "cannot read"):
                Verdict.load(self._verdict(workspace), plan=plan, registered_attempts=2,
                             expected_receipts=((receipt, "token-a"), (workspace / "r2.json", "token-b")))

    def test_worker_receipt_is_bound_to_attempt_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = self._receipt(workspace, "token-a")
            self.assertEqual(WorkerReceipt.load(path, expected_token="token-a").summary, "worker finished")
            with self.assertRaisesRegex(RecordError, "does not match"):
                WorkerReceipt.load(path, expected_token="token-b")

    def test_accepted_verdict_rejects_missing_worker_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            receipt = self._receipt(workspace, "token-a")
            (workspace / "result.md").unlink()
            with self.assertRaisesRegex(RecordError, "cannot read worker result"):
                Verdict.load(self._verdict(workspace), plan=self._plan(workspace),
                             expected_receipts=((receipt, "token-a"),))

    def test_receipt_requires_nonempty_utf8_result_for_both_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for status in ("ready_for_verification", "blocked"):
                for content in (b"", b" \n\t", b"\xff"):
                    with self.subTest(status=status, content=content):
                        receipt = self._receipt(workspace, "token-a", status)
                        (workspace / "result.md").write_bytes(content)
                        with self.assertRaises(RecordError):
                            WorkerReceipt.load(receipt, expected_token="token-a")


if __name__ == "__main__":
    unittest.main()
