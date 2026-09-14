from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from conductor.agents.base import submit, wait_for_receipt, wait_until_ready
from conductor.agents.codex import CODEX
from conductor.models import WorkerReceipt


class DelayedMenuSession:
    def __init__(
        self,
        receipt: Path | None = None,
        token: str = "token-a",
        *,
        initial_ready: bool = False,
    ) -> None:
        self.receipt = receipt
        self.token = token
        self.name = "fixture-session"
        self.capture_count = 0
        self.menu_open = True
        self.keys: list[str] = []
        self.submissions: list[str] = []
        self.initial_ready = initial_ready

    def capture(self) -> str:
        self.capture_count += 1
        if self.initial_ready and self.capture_count == 1:
            return "› Ask Codex to do anything"
        if self.menu_open:
            return "› 1. Yes, continue\nPress enter to continue"
        return "› Ask Codex to do anything"

    def status(self) -> object:
        return SimpleNamespace(pane_dead=False)

    def send_keys(self, key: str) -> None:
        self.keys.append(key)
        if key == "Enter":
            self.menu_open = False

    def send_text(self, text: str) -> None:
        self.submissions.append(text)
        if self.receipt is not None:
            self.receipt.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "token": self.token,
                        "status": "ready_for_verification",
                        "summary": "done",
                    }
                ),
                encoding="utf-8",
            )


class AgentControllerTests(unittest.TestCase):
    def test_startup_handles_menu_before_declaring_stable_ready(self) -> None:
        session = DelayedMenuSession(initial_ready=True)
        with mock.patch("conductor.agents.base.time.sleep", return_value=None):
            wait_until_ready(
                session,  # type: ignore[arg-type]
                CODEX,
                timeout_seconds=1,
                ready_settle_seconds=0,
            )
        self.assertIn("Enter", session.keys)

    def test_submission_is_fully_replayed_after_delayed_menu(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "receipt.json"
            session = DelayedMenuSession(receipt)
            with mock.patch("conductor.agents.base.time.sleep", return_value=None):
                result, _elapsed = wait_for_receipt(
                    session,  # type: ignore[arg-type]
                    adapter=CODEX,
                    receipt_file=receipt,
                    token="token-a",
                    timeout_seconds=1,
                    submission="完整任务和交接协议",
                )
            self.assertEqual(result.status, "ready_for_verification")
            self.assertEqual(session.submissions, ["完整任务和交接协议"])
            self.assertIn("C-c", session.keys)

    def test_submit_sends_one_bounded_backup_enter_after_long_paste(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "task.md"
            task.write_text("完成一个较长任务", encoding="utf-8")
            session = DelayedMenuSession()
            session.menu_open = False
            with (
                mock.patch("conductor.agents.base.time.sleep", return_value=None),
                mock.patch(
                    "conductor.agents.base.wait_for_receipt",
                    return_value=(WorkerReceipt("token-a", "ready_for_verification", "done"), 1.0),
                ),
            ):
                submit(
                    session=session,  # type: ignore[arg-type]
                    adapter=CODEX,
                    task_file=task,
                    receipt_file=root / "receipt.json",
                    token="token-a",
                    script=Path("/skill/codex_session.py"),
                    timeout_seconds=1,
                )
            self.assertEqual(session.keys, ["Enter"])
            self.assertIn(str(task), session.submissions[0])
            self.assertNotIn("完成一个较长任务", session.submissions[0])


if __name__ == "__main__":
    unittest.main()
