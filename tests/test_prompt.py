from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conductor.models import AgentKind
from conductor.prompt import build_prompt
from conductor.state import RunState


class PromptTests(unittest.TestCase):
    def test_manager_contract_contains_both_agent_commands_and_plan_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            state = RunState.create(
                state_root=root / "state",
                workspace=workspace,
                prompt="请使用 Codex 修改文件。验收标准：测试通过。",
                available_agents=set(AgentKind),
                max_attempts=2,
                worker_idle_timeout_seconds=60,
                keep_session=False,
            )
            prompt = build_prompt(
                state,
                skill_scripts={
                    AgentKind.CLAUDE: Path("/skills/claude_session.py"),
                    AgentKind.CODEX: Path("/skills/codex_session.py"),
                },
                available_agents=set(AgentKind),
                max_attempts=2,
                worker_idle_timeout_seconds=60,
                keep_session=False,
            )
            self.assertIn("claude_session.py run", prompt)
            self.assertIn("codex_session.py run", prompt)
            self.assertIn(str(state.plan_file), prompt)
            self.assertIn("验收项 ID 必须唯一", prompt)
            self.assertIn("不能在中途切换 agent", prompt)
            self.assertNotIn("include_report=true", prompt)
            full = build_prompt(state, skill_scripts={kind: Path("/skill/controller.py") for kind in AgentKind},
                                available_agents=set(AgentKind), max_attempts=2, worker_idle_timeout_seconds=60,
                                keep_session=False, include_report=True)
            self.assertIn("subtask-reports.json", full)
            self.assertIn("没有内部子任务也必须写空清单", full)
            self.assertIn("不逐项展示检查结果", full)
            self.assertIn("verdict.checks 完整记录每项检查", full)
            self.assertIn("verdict.summary 用一两句话", full)
            self.assertIn("不逐项复述检查方法、证据或产物列表", full)


if __name__ == "__main__":
    unittest.main()
