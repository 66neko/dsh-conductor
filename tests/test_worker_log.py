from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from conductor.tmux import TmuxSession
from conductor.worker_log import WorkerLogFollower, changed_screen_lines, normalize_screen


class ScreenDiffTests(unittest.TestCase):
    def test_screen_diff_only_returns_new_or_replaced_lines(self) -> None:
        previous = ("header", "working", "footer")
        current = ("header", "step one complete", "step two running", "footer")
        self.assertEqual(
            changed_screen_lines(previous, current, max_lines=10),
            ("step one complete", "step two running"),
        )

    def test_normalize_screen_removes_empty_and_control_characters(self) -> None:
        self.assertEqual(normalize_screen("\nhello\x07  \n   \nworld\n"), ("hello", "world"))

    def test_normalize_screen_suppresses_spinner_churn_and_handoff_text(self) -> None:
        first = normalize_screen(
            "• Working (7s • esc to interrupt)\n"
            "python3.13 /repo/codex_session.py complete --receipt /tmp/receipt.json\n"
        )
        second = normalize_screen("◦ Working (1m 08s • esc to interrupt)\n")
        self.assertEqual(first, ("* Working (... • esc to interrupt)",))
        self.assertEqual(second, first)

    def test_normalize_screen_hides_handoff_block_and_wrapped_token(self) -> None:
        token = "a" * 48
        screen = (
            "visible before\n"
            "<dsh_conductor_handoff>\n"
            f"complete --receipt /tmp/r --token {token}\n"
            "</dsh_conductor_handoff>\n"
            f"wrapped secret {token}\n"
            "visible after\n"
        )
        self.assertEqual(normalize_screen(screen), ("visible before", "visible after"))


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class WorkerLogFollowerTests(unittest.TestCase):
    def test_follower_streams_and_persists_tmux_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_name = f"dsh-log-test-{time.monotonic_ns()}"[-60:]
            session = TmuxSession.create(
                name=session_name,
                workspace=root,
                agent="fixture",
                command=[
                    "bash",
                    "-lc",
                    "printf 'phase one\\n'; sleep 0.4; printf 'phase two\\n'; sleep 3",
                ],
            )
            events: list[tuple[str, tuple[str, ...]]] = []
            log_file = root / "worker-screen.log"
            follower = WorkerLogFollower(
                session_name=session_name,
                agent="fixture",
                log_file=log_file,
                sink=lambda agent, lines: events.append((agent, tuple(lines))),
                interval_seconds=0.1,
            ).start()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if log_file.exists() and "phase two" in log_file.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.05)
                persisted = log_file.read_text(encoding="utf-8")
                self.assertIn("phase one", persisted)
                self.assertIn("phase two", persisted)
                self.assertTrue(any(agent == "fixture" for agent, _lines in events))
            finally:
                follower.stop()
                session.close()


if __name__ == "__main__":
    unittest.main()
