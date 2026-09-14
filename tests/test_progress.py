from __future__ import annotations

import unittest

from conductor.progress import ProgressReporter, RunEvent


class ProgressTests(unittest.TestCase):
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
