from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from conductor.models import AgentKind, RecordError, Verdict, WorkerReceipt


class RecordTests(unittest.TestCase):
    def test_accepted_verdict_requires_real_artifacts_and_passing_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "artifact.txt").write_text("ok", encoding="utf-8")
            verdict_path = workspace / "verdict.json"
            verdict_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "status": "accepted",
                        "agent": "claude",
                        "attempts": 1,
                        "artifacts": ["artifact.txt"],
                        "checks": [
                            {
                                "criterion": "file content",
                                "method": "read artifact.txt",
                                "evidence": "content was ok",
                                "passed": True,
                            }
                        ],
                        "summary": "accepted",
                        "remaining_issues": [],
                    }
                ),
                encoding="utf-8",
            )
            verdict = Verdict.load(
                verdict_path,
                expected_run_id="run-1",
                expected_agent=AgentKind.CLAUDE,
                max_attempts=2,
                workspace=workspace,
                expected_receipts=((self._receipt(workspace, "token-a"), "token-a"),),
            )
            self.assertEqual(verdict.status, "accepted")

            (workspace / "artifact.txt").unlink()
            with self.assertRaisesRegex(RecordError, "does not exist"):
                Verdict.load(
                    verdict_path,
                    expected_run_id="run-1",
                    expected_agent=AgentKind.CLAUDE,
                    max_attempts=2,
                    workspace=workspace,
                    expected_receipts=((workspace / "receipt.json", "token-a"),),
                )

    @staticmethod
    def _receipt(workspace: Path, token: str, status: str = "ready_for_verification") -> Path:
        path = workspace / "receipt.json"
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

    def test_accepted_verdict_requires_bound_final_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "artifact.txt").write_text("ok", encoding="utf-8")
            verdict_path = workspace / "verdict.json"
            verdict_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "run-1",
                        "status": "accepted",
                        "agent": "codex",
                        "attempts": 1,
                        "artifacts": ["artifact.txt"],
                        "checks": [
                            {
                                "criterion": "content",
                                "method": "read",
                                "evidence": "ok",
                                "passed": True,
                            }
                        ],
                        "summary": "accepted",
                        "remaining_issues": [],
                    }
                ),
                encoding="utf-8",
            )
            receipt = self._receipt(workspace, "token-a", status="blocked")
            with self.assertRaisesRegex(RecordError, "ready_for_verification"):
                Verdict.load(
                    verdict_path,
                    expected_run_id="run-1",
                    expected_agent=AgentKind.CODEX,
                    max_attempts=1,
                    workspace=workspace,
                    expected_receipts=((receipt, "token-a"),),
                )

    def test_worker_receipt_is_bound_to_attempt_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "token": "token-a",
                        "status": "ready_for_verification",
                        "summary": "done",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(WorkerReceipt.load(path, expected_token="token-a").summary, "done")
            with self.assertRaisesRegex(RecordError, "does not match"):
                WorkerReceipt.load(path, expected_token="token-b")


if __name__ == "__main__":
    unittest.main()
