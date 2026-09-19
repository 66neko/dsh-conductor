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
                    "implementation_steps": ["创建文件", "运行检查"],
                    "acceptance_criteria": [
                        {"id": "content", "description": "文件内容正确"},
                        {"id": "tests", "description": "测试通过"},
                    ],
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
    def _verdict(workspace: Path, *, check_ids: tuple[str, ...] = ("content", "tests")) -> Path:
        path = workspace / "verdict.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "run_id": "run-1",
                    "status": "accepted",
                    "agent": "codex",
                    "attempts": 1,
                    "artifacts": ["artifact.txt"],
                    "checks": [
                        {
                            "criterion_id": criterion_id,
                            "criterion": criterion_id,
                            "method": "独立检查",
                            "evidence": "通过",
                            "passed": True,
                        }
                        for criterion_id in check_ids
                    ],
                    "summary": "accepted",
                    "remaining_issues": [],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_accepted_verdict_requires_artifacts_checks_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "artifact.txt").write_text("ok", encoding="utf-8")
            plan = self._plan(workspace)
            receipt = self._receipt(workspace, "token-a")
            verdict = Verdict.load(
                self._verdict(workspace),
                plan=plan,
                max_attempts=2,
                workspace=workspace,
                expected_receipts=((receipt, "token-a"),),
            )
            self.assertEqual(verdict.status, "accepted")

            (workspace / "artifact.txt").unlink()
            with self.assertRaisesRegex(RecordError, "does not exist"):
                Verdict.load(
                    workspace / "verdict.json",
                    plan=plan,
                    max_attempts=2,
                    workspace=workspace,
                    expected_receipts=((receipt, "token-a"),),
                )

    def test_verdict_must_cover_each_planned_criterion_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "artifact.txt").write_text("ok", encoding="utf-8")
            plan = self._plan(workspace)
            receipt = self._receipt(workspace, "token-a")
            with self.assertRaisesRegex(RecordError, "cover every"):
                Verdict.load(
                    self._verdict(workspace, check_ids=("content",)),
                    plan=plan,
                    max_attempts=1,
                    workspace=workspace,
                    expected_receipts=((receipt, "token-a"),),
                )

    def test_accepted_verdict_requires_bound_final_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "artifact.txt").write_text("ok", encoding="utf-8")
            receipt = self._receipt(workspace, "token-a", status="blocked")
            with self.assertRaisesRegex(RecordError, "ready_for_verification"):
                Verdict.load(
                    self._verdict(workspace),
                    plan=self._plan(workspace),
                    max_attempts=1,
                    workspace=workspace,
                    expected_receipts=((receipt, "token-a"),),
                )

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
            (workspace / "artifact.txt").write_text("ok", encoding="utf-8")
            receipt = self._receipt(workspace, "token-a")
            (workspace / "result.md").unlink()
            with self.assertRaisesRegex(RecordError, "cannot read worker result"):
                Verdict.load(
                    self._verdict(workspace),
                    plan=self._plan(workspace),
                    max_attempts=1,
                    workspace=workspace,
                    expected_receipts=((receipt, "token-a"),),
                )

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
