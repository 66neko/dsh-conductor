"""Codex 的 tmux 启动与就绪协议。"""

from __future__ import annotations

import re

from .base import AgentAdapter, main as adapter_main


# pending_submission 是 Codex 长文本粘贴尚未提交的明确界面事实。
CODEX = AgentAdapter(
    kind="codex",
    executable="codex",
    arguments=("--dangerously-bypass-approvals-and-sandbox", "--no-alt-screen"),
    cursor_glyphs=("›",),
    menu_hint=re.compile(r"Press enter to continue|to navigate|to select|\[y/n\]|\(y/n\)", re.IGNORECASE),
    affirmative=re.compile(r"^(yes|continue|allow|approve)\b", re.IGNORECASE),
    ready=re.compile(r"›|permissions:\s*YOLO", re.IGNORECASE),
    pending_submission=re.compile(r"^› \[Pasted Content \d+ chars\]", re.MULTILINE),
)


def main(argv: list[str] | None = None) -> int:
    return adapter_main(CODEX, argv)
