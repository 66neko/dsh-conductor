from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Sequence
from unittest import mock

from conductor.tmux import TmuxSession
from conductor.worker_log import WorkerLogFollower, changed_screen_lines, normalize_screen


class ScreenDiffTests(unittest.TestCase):
    def test_large_repeated_history_preserves_tail_with_bounded_comparison(self) -> None:
        previous = tuple(['A', 'B'] * 2500)
        current = tuple(['B', 'A'] * 25000 + ['FINAL OUTPUT'])
        # 禁止退回二次复杂度算法，同时验证 50000 行补采和末行没有丢失。
        with mock.patch('conductor.worker_log.difflib.SequenceMatcher', side_effect=AssertionError('unbounded diff')):
            changed = changed_screen_lines(previous, current)
        self.assertEqual(changed, current)

    def test_persisted_diff_retains_repeated_lines(self) -> None:
        self.assertEqual(changed_screen_lines((), ("same", "same", "end")), ("same", "same", "end"))
        history = ("repeated history",) * 5000
        self.assertEqual(changed_screen_lines(history + ("working",), history + ("done",)), ("done",))

    def test_large_update_is_fully_persisted_while_callback_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch("conductor.worker_log.TmuxSession") as session_type:
            lines = tuple(f"结果第 {i} 行" for i in range(500)) + ("重复内容", "重复内容")
            session_type.return_value.exists.return_value = True
            session_type.return_value.capture.return_value = "\n".join(lines)
            events = []
            log_file = Path(directory) / "worker-screen.log"
            follower = WorkerLogFollower(
                session_name="fixture", agent="claude", log_file=log_file,
                sink=lambda _agent, update: events.append(update),
            )
            self.assertEqual(len(follower.sample_once()), 12)
            self.assertEqual(follower.sample_once(), ())
            persisted = tuple(line[2:] for line in log_file.read_text(encoding="utf-8").splitlines() if line.startswith("| "))
            self.assertEqual(persisted, lines)
            self.assertEqual(len(events), 1)

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
    def test_ten_second_poll_flushes_long_final_output_before_next_tick(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gate = root / "emit-final"
            session = TmuxSession.create(
                name=f"dsh-final-log-test-{time.monotonic_ns()}", workspace=root, agent="fixture",
                command=[
                    sys.executable, "-c",
                    "import pathlib,sys,time\n"
                    "print('phase one', flush=True)\n"
                    "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(0.01)\n"
                    "sys.stdout.write('\\n'.join(f'FINAL-{i:05d}' for i in range(6000)))\n"
                    "sys.stdout.flush()\n"
                    "time.sleep(60)\n",
                    str(gate),
                ],
            )
            first_sample = threading.Event()
            updates: list[tuple[str, ...]] = []

            def receive(_agent: str, lines: Sequence[str]) -> None:
                updates.append(tuple(lines))
                first_sample.set()

            log_file = root / "worker-screen.log"
            follower = WorkerLogFollower(
                session_name=session.name, agent="fixture", log_file=log_file, sink=receive,
            )
            try:
                deadline = time.monotonic() + 5
                while "phase one" not in session.capture() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIn("phase one", session.capture())
                follower.start()
                self.assertTrue(first_sample.wait(timeout=2))
                gate.touch()
                deadline = time.monotonic() + 5
                while "FINAL-05999" not in session.capture() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIn("FINAL-05999", session.capture())
                # 终端已经刷新，但还没到 10 秒；周期采集不能随着刷新不断触发。
                self.assertEqual(len(updates), 1)
                started = time.monotonic()
                follower.stop()
                self.assertLess(time.monotonic() - started, 2)
                persisted = [line[2:] for line in log_file.read_text(encoding="utf-8").splitlines() if line.startswith("| FINAL-")]
                self.assertEqual(persisted, [f"FINAL-{i:05d}" for i in range(6000)])
                self.assertEqual(len(updates[-1]), 12)
            finally:
                follower.stop()
                session.close()

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
