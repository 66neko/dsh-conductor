from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor.models import AgentKind
from conductor.skills import (
    available_workspace_agent_skills,
    prepare_workspace_skills,
    source_skill,
    workspace_skill_root,
)


class WorkspaceSkillTests(unittest.TestCase):
    def test_prepare_overwrites_both_skills_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace_skill_root(workspace)
            original = root / AgentKind.CLAUDE.skill_name
            original.mkdir(parents=True)
            (original / "marker.txt").write_text("user skill", encoding="utf-8")

            prepared = prepare_workspace_skills(workspace)
            self.assertEqual(prepared, root)
            with mock.patch("conductor.skills.shutil.which", return_value=None):
                scripts, available = available_workspace_agent_skills(workspace)
            self.assertEqual(set(scripts), set(AgentKind))
            self.assertEqual(
                scripts[AgentKind.CLAUDE],
                root / AgentKind.CLAUDE.skill_name / "scripts" / AgentKind.CLAUDE.script_name,
            )
            self.assertTrue((root / AgentKind.CLAUDE.skill_name / "SKILL.md").is_file())
            self.assertNotIn(AgentKind.CLAUDE, available)
            self.assertFalse((root / AgentKind.CLAUDE.skill_name / "marker.txt").exists())
            self.assertTrue((root / AgentKind.CODEX.skill_name / "SKILL.md").is_file())

    def test_scope_uses_packaged_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            prepare_workspace_skills(workspace)
            for kind in AgentKind:
                target = workspace_skill_root(workspace) / kind.skill_name
                self.assertEqual(
                    (target / "SKILL.md").read_bytes(),
                    (source_skill(kind) / "SKILL.md").read_bytes(),
                )


if __name__ == "__main__":
    unittest.main()
