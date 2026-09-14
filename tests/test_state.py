from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conductor.models import AgentKind, read_json_object
from conductor.state import RunState


class RunStateTests(unittest.TestCase):
    def test_each_run_and_attempt_has_unique_fact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            state = RunState.create(
                state_root=root / "state",
                workspace=workspace,
                agent=AgentKind.CODEX,
                task="implement it",
                acceptance="tests pass",
                max_attempts=2,
                attempt_timeout_seconds=30,
                keep_session=False,
            )
            request = read_json_object(state.request_file)
            self.assertEqual(request["run_id"], state.run_id)
            self.assertEqual(request["agent"], "codex")
            self.assertEqual(len(state.attempts), 2)
            self.assertNotEqual(state.attempts[0].token, state.attempts[1].token)
            self.assertNotEqual(state.attempts[0].receipt_file, state.attempts[1].receipt_file)
            self.assertFalse(state.verdict_file.exists())


if __name__ == "__main__":
    unittest.main()
