"""Codex 的 tmux 启动与就绪协议。"""

from __future__ import annotations

import re

from .base import AgentAdapter, main as adapter_main


class CodexAdapter(AgentAdapter):
    def reconnection_status(self, screen: str) -> str | None:
        """不依赖重试次数；自动重连状态优先于错误详情，耗尽只认终态。"""
        prompts = list(re.finditer(r"^›", screen, re.MULTILINE))
        if len(prompts) > 1:
            # 最后一个 › 是输入框，之前一个是最新用户消息；更早的状态属于历史。
            screen = screen[prompts[-2].start():]
        reconnects = list(re.finditer(
            r"^[ \t]*(?:[•●◦⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏][ \t]*)?"
            r"Reconnecting(?:\.{3}|…|\s+to\s+server)", screen, re.IGNORECASE | re.MULTILINE,
        ))
        failures = list(re.finditer(
            r"exceeded (?:stream )?retry limit|(?:retry|reconnect(?:ion)?) (?:limit|attempts|retries) exhausted|"
            r"stream disconnected before completion|failed to reconnect",
            screen, re.IGNORECASE,
        ))
        busy = list(self.busy_hint.finditer(screen))
        last_reconnect = reconnects[-1].start() if reconnects else -1
        last_failure = failures[-1].start() if failures else -1
        # 忽略更早任务的忙碌提示；当前重连行的 interrupt 提示仍表示重试在进行。
        current_busy = busy and busy[-1].start() > max(last_reconnect, last_failure)
        reconnect_busy = (reconnects and busy and busy[-1].start() > last_reconnect
                          and "\n" not in screen[last_reconnect:busy[-1].start()])
        if failures and not current_busy and not reconnect_busy and last_failure > last_reconnect:
            return "retry_exhausted"
        if reconnects:
            # --no-alt-screen 保留旧状态行；后来的 Working 状态表示自动重连已恢复。
            if busy and "\n" in screen[last_reconnect:busy[-1].start()]:
                return None
            return "reconnecting"
        return None

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
