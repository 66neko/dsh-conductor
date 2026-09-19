from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conductor.models import AgentKind, read_json_object
from conductor.state import RunState


class RunStateTests(unittest.TestCase):
    def test_run_prepares_isolated_candidates_for_both_agents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            state = RunState.create(
                state_root=root / "state",
                workspace=workspace,
                prompt="实现任务。验收标准：测试通过。",
                available_agents={AgentKind.CODEX},
                max_attempts=2,
                worker_idle_timeout_seconds=30,
                keep_session=False,
            )
            request = read_json_object(state.request_file)
            self.assertEqual(request["schema_version"], 2)
            self.assertEqual(request["run_id"], state.run_id)
            self.assertEqual(request["available_agents"], ["codex"])
            self.assertEqual(state.user_prompt_file.read_text(encoding="utf-8"), "实现任务。验收标准：测试通过。\n")
            self.assertEqual({agent.kind for agent in state.agents}, set(AgentKind))
            for kind in AgentKind:
                attempts = state.agent_state(kind).attempts
                self.assertEqual(len(attempts), 2)
                self.assertNotEqual(attempts[0].token, attempts[1].token)
                self.assertNotEqual(attempts[0].receipt_file, attempts[1].receipt_file)
                self.assertNotEqual(attempts[0].result_file, attempts[1].result_file)
                for attempt in attempts:
                    self.assertEqual(attempt.result_file.parent, attempt.receipt_file.parent)
                    self.assertEqual(attempt.to_json()["result_file"], str(attempt.result_file))
                    self.assertFalse(attempt.result_file.exists())
            self.assertNotEqual(
                state.agent_state(AgentKind.CLAUDE).session,
                state.agent_state(AgentKind.CODEX).session,
            )
            self.assertFalse(state.plan_file.exists())
            self.assertFalse(state.verdict_file.exists())


if __name__ == "__main__":
    unittest.main()
