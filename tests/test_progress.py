from __future__ import annotations

import unittest
import tempfile
import json
import time
from pathlib import Path

from conductor.progress import ProgressReporter, RunEvent


class ProgressTests(unittest.TestCase):
    def test_heartbeat_activity_is_persisted_without_display_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sdk-heartbeat.json'
            reporter = ProgressReporter(heartbeat_seconds=0.01, heartbeat_file=path).start()
            try:
                deadline = time.monotonic() + 2
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertGreater(json.loads(path.read_text())['last_output_at'], 0)
            finally:
                reporter.stop()

    def test_supervision_events_include_recovery_count_and_partial_lines_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'supervision.jsonl'
            events = []
            reporter = ProgressReporter(on_event=events.append, supervision_log=path, heartbeat_seconds=60).start()
            try:
                row = json.dumps({'event': 'recovery', 'recoveries': 3, 'message': '网络重试'})
                path.write_text(row)
                reporter.read_supervision()
                self.assertEqual(reporter._journal_offset, 0)
                path.write_text(row + '\n')
                reporter.read_supervision()
                reporter.read_supervision()
            finally:
                reporter.stop()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, 'supervision_recovery')
            self.assertIn('3/5', events[0].message)

    def test_dsh_and_worker_output_share_structured_event_interface(self) -> None:
        events: list[RunEvent] = []
        reporter = ProgressReporter(on_event=events.append, heartbeat_seconds=60).start()
        try:
            reporter({"type": "tool/call", "data": {"name": "bash", "arguments": {"command": "pwd"}}})
            reporter.worker_lines("codex", ("running tests",))
        finally:
            reporter.stop()
        self.assertEqual(events[0].kind, "tool_call")
        self.assertEqual(events[1].source, "codex")
        self.assertIn("codex | running tests", events[1].format())
        self.assertEqual(events[1].to_json()["message"], "running tests")


if __name__ == "__main__":
    unittest.main()
