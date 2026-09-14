"""Claude Code 的 tmux 启动与就绪协议。"""

from __future__ import annotations

import re

from .base import AgentAdapter, main as adapter_main


# 菜单与就绪表达式只描述 Claude Code，不能与 Codex 的界面规则混用。
CLAUDE = AgentAdapter(
    kind="claude",
    executable="claude",
    arguments=("--dangerously-skip-permissions",),
    cursor_glyphs=("❯",),
    menu_hint=re.compile(r"Enter to confirm|Esc to cancel|to navigate|to select", re.IGNORECASE),
    affirmative=re.compile(r"^(yes|trust|accept|continue|allow|approve)\b", re.IGNORECASE),
    ready=re.compile(r"❯|bypass permissions on", re.IGNORECASE),
)


def main(argv: list[str] | None = None) -> int:
    return adapter_main(CLAUDE, argv)
