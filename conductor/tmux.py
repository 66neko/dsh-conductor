"""供不同 agent 控制器共用的精确 tmux 传输层。"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class TmuxError(RuntimeError):
    """tmux 操作失败，或操作目标不是预期会话。"""


_SESSION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _run(
    arguments: Sequence[str],
    *,
    input_text: str | None = None,
    timeout_seconds: float = 20.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["tmux", *arguments],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TmuxError(f"tmux invocation failed: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise TmuxError(f"tmux {' '.join(arguments)} failed: {detail}")
    return result


def validate_session_name(name: str) -> str:
    if not _SESSION_NAME.fullmatch(name):
        raise TmuxError("session name must match [A-Za-z0-9_-]{1,64}")
    return name


@dataclass(frozen=True, slots=True)
class SessionStatus:
    name: str
    agent: str
    workspace: str
    pane_dead: bool
    pane_pid: int | None

    def to_json(self) -> dict[str, object]:
        return {
            "session": self.name,
            "agent": self.agent,
            "workspace": self.workspace,
            "pane_dead": self.pane_dead,
            "pane_pid": self.pane_pid,
            "attach_command": f"tmux attach -t {self.name}",
        }


class TmuxSession:
    """带 conductor 元数据标记的分离式 tmux 会话。"""

    def __init__(self, name: str) -> None:
        self.name = validate_session_name(name)

    @classmethod
    def create(
        cls,
        *,
        name: str,
        workspace: Path,
        agent: str,
        command: Sequence[str],
        width: int = 200,
        height: int = 50,
    ) -> "TmuxSession":
        session = cls(name)
        if session.exists():
            raise TmuxError(f"tmux session already exists: {name}")
        root = workspace.expanduser().resolve()
        if not root.is_dir():
            raise TmuxError(f"workspace is not a directory: {root}")
        if not command:
            raise TmuxError("agent command is empty")
        shell_command = "exec " + shlex.join(command)
        arguments = [
            "new-session",
            "-d",
            "-s",
            name,
            "-x",
            str(width),
            "-y",
            str(height),
            "-c",
            str(root),
        ]
        if path_value := os.environ.get("PATH"):
            arguments.extend(["-e", f"PATH={path_value}"])
        arguments.append(shell_command)
        _run(arguments)
        try:
            # agent 与工作区写入会话元数据，后续控制器据此拒绝误接管。
            _run(["set-option", "-p", "-t", name, "remain-on-exit", "on"])
            _run(["set-option", "-t", name, "history-limit", "50000"])
            _run(["set-option", "-t", name, "@dsh_conductor_agent", agent])
            _run(["set-option", "-t", name, "@dsh_conductor_workspace", str(root)])
        except Exception:
            session.close()
            raise
        return session

    @classmethod
    def attach(cls, *, name: str, expected_agent: str) -> "TmuxSession":
        session = cls(name)
        if not session.exists():
            raise TmuxError(f"tmux session does not exist: {name}")
        actual = session._option("@dsh_conductor_agent")
        if actual != expected_agent:
            raise TmuxError(
                f"session {name!r} belongs to {actual or 'an unknown process'}, not {expected_agent}"
            )
        return session

    def exists(self) -> bool:
        return _run(["has-session", "-t", self.name], check=False).returncode == 0

    def _option(self, name: str) -> str:
        return _run(["show-option", "-qv", "-t", self.name, name]).stdout.strip()

    def capture(self, *, history_lines: int = 0) -> str:
        arguments = ["capture-pane", "-p", "-J", "-t", self.name]
        if history_lines > 0:
            arguments.extend(["-S", f"-{history_lines}"])
        return _run(arguments, timeout_seconds=30.0).stdout

    def send_text(
        self,
        text: str,
        *,
        submit: bool = True,
        submit_delay_seconds: float = 1.0,
    ) -> None:
        # 使用 tmux buffer 传递字面文本，避免任务内容被 shell 或 send-keys 解释。
        buffer_name = f"dsh-{self.name}-{os.getpid()}-{time.monotonic_ns()}"
        _run(["load-buffer", "-b", buffer_name, "-"], input_text=text)
        try:
            _run(["paste-buffer", "-d", "-b", buffer_name, "-t", self.name])
        finally:
            _run(["delete-buffer", "-b", buffer_name], check=False)
        if submit:
            # 全屏 TUI 会异步处理 bracketed paste；立即发送 Enter 可能早于粘贴完成，
            # 导致完整 prompt 仍停留在编辑器中而没有提交。
            time.sleep(submit_delay_seconds)
            self.send_keys("Enter")

    def send_keys(self, *keys: str) -> None:
        if keys:
            _run(["send-keys", "-t", self.name, *keys])

    def status(self) -> SessionStatus:
        fields = _run(
            ["display-message", "-p", "-t", self.name, "#{pane_dead}\t#{pane_pid}"],
        ).stdout.strip().split("\t")
        pane_pid = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None
        return SessionStatus(
            name=self.name,
            agent=self._option("@dsh_conductor_agent"),
            workspace=self._option("@dsh_conductor_workspace"),
            pane_dead=bool(fields and fields[0] == "1"),
            pane_pid=pane_pid,
        )

    def close(self) -> None:
        _run(["kill-session", "-t", self.name], check=False)

    @staticmethod
    def list_tagged() -> list[SessionStatus]:
        result = _run(["list-sessions", "-F", "#{session_name}"], check=False)
        if result.returncode != 0:
            return []
        sessions: list[SessionStatus] = []
        for name in result.stdout.splitlines():
            if not name.strip():
                continue
            session = TmuxSession(name.strip())
            if session._option("@dsh_conductor_agent"):
                sessions.append(session.status())
        return sessions
