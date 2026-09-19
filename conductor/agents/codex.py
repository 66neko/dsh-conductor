"""Codex 的 tmux 启动与就绪协议。"""

from __future__ import annotations

import re

from .base import AgentAdapter, main as adapter_main


class CodexAdapter(AgentAdapter):
    def submission_status(self, screen: str, activity_status: str) -> str:
        """只检查光标所在的最后一个输入框，历史用户消息也使用 ›，不能混淆。"""
        try:
            x, y, visible = (int(value) for value in activity_status.split(":")[:3])
        except ValueError:
            return "unknown"
        lines = screen.split("\n")
        prompts = [i for i, line in enumerate(lines) if line.startswith("›")]
        if not prompts or not 0 <= y < len(lines) or visible != 1:
            return "unknown"
        start = prompts[-1]
        if y < start:
            return "unknown"
        draft = "\n".join(lines[start:])
        # 多行展开、折叠粘贴和普通文字都要识别。空输入框的 placeholder
        # 虽然有文字，但光标仍在 › 后的第 2 列；换行后的光标也属于草稿。
        if (x > 2 or y > start or "<dsh_conductor_handoff>" in draft
                or "</dsh_conductor_handoff>" in draft or "[Pasted Content" in draft):
            return "pending"
        return "empty" if x == 2 else "unknown"

    def has_pending_submission(self, screen: str, activity_status: str = "") -> bool:
        return self.submission_status(screen, activity_status) == "pending"


CODEX = CodexAdapter(
    kind="codex",
    executable="codex",
    arguments=("--dangerously-bypass-approvals-and-sandbox", "--no-alt-screen"),
    cursor_glyphs=("›",),
    menu_hint=re.compile(r"Press enter to continue|to navigate|to select|\[y/n\]|\(y/n\)", re.IGNORECASE),
    affirmative=re.compile(r"^(yes|continue|allow|approve)\b", re.IGNORECASE),
    ready=re.compile(r"›|permissions:\s*YOLO", re.IGNORECASE),
    confirm_submission=True,
)


def main(argv: list[str] | None = None) -> int:
    return adapter_main(CODEX, argv)
