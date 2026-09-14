from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

from conductor.dsh import DshClient, DshConfig, DshError, resolve_node_bin


class DshClientTests(unittest.TestCase):
    def test_explicit_missing_node_is_rejected(self) -> None:
        with mock.patch.dict("os.environ", {"DSH_NODE": "/does/not/exist"}):
            with self.assertRaisesRegex(DshError, "DSH_NODE"):
                resolve_node_bin()

    def test_turn_completion_accepts_both_notification_orders(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        for order in ("turn-first", "idle-first"):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                config = DshConfig(
                    workspace=Path(directory),
                    dsh_bin=str(fixture),
                    extra_env={"FAKE_DSH_ORDER": order},
                )
                with DshClient(config) as client:
                    result = client.run("test", session_id="fixture", timeout_seconds=2)
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.final_text, "fixture complete")
                self.assertEqual(result.turn_end_reason, {"kind": "completed"})

    def test_early_exit_reports_code_and_stderr(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        with tempfile.TemporaryDirectory() as directory:
            config = DshConfig(
                workspace=Path(directory),
                dsh_bin=str(fixture),
                extra_env={"FAKE_DSH_EXIT_EARLY": "1"},
            )
            with self.assertRaises(DshError) as caught, DshClient(config) as client:
                client.run("test", session_id="fixture", timeout_seconds=2)
        message = str(caught.exception)
        self.assertIn("code 17", message)
        self.assertIn("fixture startup failure", message)

    def test_permission_mode_cannot_be_overridden_by_extra_environment(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_dsh.py"
        with tempfile.TemporaryDirectory() as directory:
            config = DshConfig(
                workspace=Path(directory),
                dsh_bin=str(fixture),
                extra_env={
                    "FAKE_DSH_CHECK_PERMISSION": "1",
                    "DSH_PERMISSION_MODE": "sandbox",
                },
            )
            with DshClient(config) as client:
                result = client.run("test", session_id="fixture", timeout_seconds=2)
        self.assertEqual(result.status, "completed")


if __name__ == "__main__":
    unittest.main()
