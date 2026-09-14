from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from conductor.tmux import TmuxError, TmuxSession


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TmuxTransportTests(unittest.TestCase):
    def test_buffer_transport_preserves_literal_text_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "input.txt"
            session_name = f"dsh-test-{time.monotonic_ns()}"[-60:]
            code = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(input(), encoding='utf-8')"
            session = TmuxSession.create(
                name=session_name,
                workspace=root,
                agent="fixture",
                command=[sys.executable, "-c", code, str(output)],
            )
            try:
                payload = "literal $HOME $(date) Enter 'quoted'"
                session.send_text(payload, submit_delay_seconds=0)
                deadline = time.monotonic() + 5
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(output.read_text(encoding="utf-8"), payload)
                status = session.status()
                self.assertEqual(status.agent, "fixture")
                self.assertEqual(status.workspace, str(root.resolve()))
                with self.assertRaises(TmuxError):
                    TmuxSession.attach(name=session_name, expected_agent="other")
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
