"""共享控制器机制；所有 TUI 差异由具体 adapter 负责。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Pattern, Sequence

from ..models import RecordError, WorkerReceipt
from ..state import atomic_write_json
from ..tmux import TmuxError, TmuxSession


class WorkerError(RuntimeError):
    """worker 会话未能到达可验证的交接点。"""


@dataclass(frozen=True, slots=True)
class AgentAdapter:
    kind: str
    executable: str
    arguments: tuple[str, ...]
    cursor_glyphs: tuple[str, ...]
    menu_hint: Pattern[str]
    affirmative: Pattern[str]
    ready: Pattern[str]
    pending_submission: Pattern[str] | None = None

    def resolve_command(self, explicit_binary: str | None = None) -> list[str]:
        raw = explicit_binary or shutil.which(self.executable)
        if not raw:
            raise WorkerError(f"cannot find {self.executable} on PATH; pass --binary")
        binary = Path(raw).expanduser().resolve()
        if not binary.is_file():
            raise WorkerError(f"agent executable does not exist: {binary}")
        return [str(binary), *self.arguments]

    def cursor_label(self, screen: str) -> str | None:
        for line in screen.splitlines():
            for glyph in self.cursor_glyphs:
                if glyph in line:
                    label = line.split(glyph, 1)[1].strip()
                    return re.sub(r"^\d+[.)]\s*", "", label)
        return None

    def is_menu(self, screen: str) -> bool:
        return bool(self.menu_hint.search(screen))

    def is_ready(self, screen: str) -> bool:
        return not self.is_menu(screen) and bool(self.ready.search(screen))

    def has_pending_submission(self, screen: str) -> bool:
        return self.pending_submission is not None and bool(self.pending_submission.search(screen))


def _emit(value: object) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _completion_prompt(*, script: Path, receipt: Path, token: str) -> str:
    # worker 的自然语言回复不参与完成判断，最后一个工具动作必须写入 token 回执。
    command = (
        f"python3.13 {shlex.quote(str(script))} complete "
        f"--receipt {shlex.quote(str(receipt))} --token {shlex.quote(token)} "
        "--status ready_for_verification --summary 'brief factual summary'"
    )
    blocked = command.replace("ready_for_verification", "blocked")
    return f"""

<dsh_conductor_handoff>
Complete the requested work autonomously. Do not treat this handoff as an acceptance test.
As your final tool action, after all edits and checks are finished, run this command with the
summary placeholder replaced by a short factual summary:

{command}

If an external blocker prevents completion, use this form instead and describe the blocker:

{blocked}

Do not perform more work after writing the receipt. The receipt only tells the independent DSH
manager that your turn is ready for verification; it does not claim that the work was accepted.
</dsh_conductor_handoff>
""".strip()


def wait_until_ready(
    session: TmuxSession,
    adapter: AgentAdapter,
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    menu_steps = 0
    while time.monotonic() < deadline:
        screen = session.capture()
        status = session.status()
        if status.pane_dead:
            raise WorkerError(f"{adapter.kind} exited during startup\n{screen[-2000:]}")
        if adapter.is_menu(screen):
            label = adapter.cursor_label(screen)
            if label and adapter.affirmative.search(label):
                session.send_keys("Enter")
                time.sleep(1.0)
                menu_steps = 0
                continue
            if menu_steps >= 8:
                raise WorkerError(f"cannot find an affirmative startup option\n{screen[-2000:]}")
            menu_steps += 1
            session.send_keys("Down")
            # Claude Code 异步重绘菜单；过早读屏会看到旧光标并误判为没有移动。
            time.sleep(0.75)
            continue
        if adapter.is_ready(screen):
            return
        time.sleep(0.5)
    raise WorkerError(f"{adapter.kind} did not become ready\n{session.capture()[-2000:]}")


def wait_for_receipt(
    session: TmuxSession,
    *,
    adapter: AgentAdapter,
    receipt_file: Path,
    token: str,
    timeout_seconds: float,
) -> tuple[WorkerReceipt, float]:
    started = time.monotonic()
    deadline = started + timeout_seconds
    last_error: str | None = None
    menu_steps = 0
    resubmit_after_menu = False
    last_submit = time.monotonic()
    while time.monotonic() < deadline:
        if receipt_file.exists():
            try:
                receipt = WorkerReceipt.load(receipt_file, expected_token=token)
                return receipt, time.monotonic() - started
            except RecordError as exc:
                last_error = str(exc)
        screen = session.capture()
        if adapter.is_menu(screen):
            label = adapter.cursor_label(screen)
            if label and adapter.affirmative.search(label):
                session.send_keys("Enter")
                menu_steps = 0
                resubmit_after_menu = True
                time.sleep(1.0)
                continue
            if menu_steps >= 8:
                raise WorkerError(f"cannot handle worker menu while waiting for receipt\n{screen[-2000:]}")
            session.send_keys("Down")
            menu_steps += 1
            time.sleep(0.75)
            continue
        if resubmit_after_menu and adapter.is_ready(screen):
            # 原本用于提交 prompt 的 Enter 可能被首次使用菜单消费；菜单关闭后补交一次。
            session.send_keys("Enter")
            resubmit_after_menu = False
            last_submit = time.monotonic()
            time.sleep(0.5)
            continue
        if adapter.has_pending_submission(screen) and time.monotonic() - last_submit >= 2.0:
            # Codex 可能在 Enter 到达后才完成 bracketed paste。仅当编辑器仍显示
            # pending-paste 标记时节流重发，避免无依据地重复提交任务。
            session.send_keys("Enter")
            last_submit = time.monotonic()
            time.sleep(0.5)
            continue
        if session.status().pane_dead:
            raise WorkerError(f"worker process exited before writing its receipt\n{screen[-2000:]}")
        time.sleep(0.5)
    detail = f"; last receipt error: {last_error}" if last_error else ""
    raise WorkerError(f"timed out waiting for worker receipt {receipt_file}{detail}")


def submit(
    *,
    session: TmuxSession,
    adapter: AgentAdapter,
    task_file: Path,
    receipt_file: Path,
    token: str,
    script: Path,
    timeout_seconds: float,
) -> dict[str, object]:
    if receipt_file.exists():
        raise WorkerError(f"refusing stale receipt path: {receipt_file}")
    try:
        task = task_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkerError(f"cannot read task file {task_file}: {exc}") from exc
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt = task.rstrip() + "\n\n" + _completion_prompt(
        script=script,
        receipt=receipt_file.resolve(),
        token=token,
    )
    session.send_text(prompt)
    receipt, elapsed = wait_for_receipt(
        session,
        adapter=adapter,
        receipt_file=receipt_file,
        token=token,
        timeout_seconds=timeout_seconds,
    )
    return {
        "schema_version": 1,
        "session": session.name,
        "agent": adapter.kind,
        "status": receipt.status,
        "summary": receipt.summary,
        "receipt_file": str(receipt_file.resolve()),
        "elapsed_seconds": round(elapsed, 3),
        "attach_command": f"tmux attach -t {session.name}",
    }


def build_parser(adapter: AgentAdapter) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{adapter.kind}_session.py",
        description=f"Control one {adapter.kind} interactive session through tmux.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="start a session, submit a task, and wait for its receipt")
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--session", required=True)
    run.add_argument("--task-file", type=Path, required=True)
    run.add_argument("--receipt", type=Path, required=True)
    run.add_argument("--token", required=True)
    run.add_argument("--binary")
    run.add_argument("--startup-timeout-seconds", type=float, default=120.0)
    run.add_argument("--timeout-seconds", type=float, default=1200.0)

    send = commands.add_parser("send", help="submit a follow-up to the same session")
    send.add_argument("--session", required=True)
    send.add_argument("--task-file", type=Path, required=True)
    send.add_argument("--receipt", type=Path, required=True)
    send.add_argument("--token", required=True)
    send.add_argument("--timeout-seconds", type=float, default=1200.0)

    for name in ("status", "capture", "close"):
        command = commands.add_parser(name)
        command.add_argument("--session", required=True)
    capture = commands.choices["capture"]
    capture.add_argument("--history-lines", type=int, default=0)

    complete = commands.add_parser("complete", help="atomically write a worker receipt")
    complete.add_argument("--receipt", type=Path, required=True)
    complete.add_argument("--token", required=True)
    complete.add_argument(
        "--status",
        choices=("ready_for_verification", "blocked"),
        required=True,
    )
    complete.add_argument("--summary", required=True)
    return parser


def main(adapter: AgentAdapter, argv: Sequence[str] | None = None) -> int:
    parser = build_parser(adapter)
    args = parser.parse_args(argv)
    script = Path(sys.argv[0]).resolve()
    try:
        if args.command == "complete":
            if args.receipt.exists():
                raise WorkerError(f"receipt already exists: {args.receipt}")
            atomic_write_json(
                args.receipt.resolve(),
                WorkerReceipt(args.token, args.status, args.summary).to_json(),
            )
            _emit({"schema_version": 1, "status": "receipt_written", "receipt": str(args.receipt)})
            return 0
        if args.command == "run":
            session = TmuxSession.create(
                name=args.session,
                workspace=args.workspace,
                agent=adapter.kind,
                command=adapter.resolve_command(args.binary),
            )
            try:
                wait_until_ready(session, adapter, timeout_seconds=args.startup_timeout_seconds)
                _emit(
                    submit(
                        session=session,
                        adapter=adapter,
                        task_file=args.task_file,
                        receipt_file=args.receipt,
                        token=args.token,
                        script=script,
                        timeout_seconds=args.timeout_seconds,
                    )
                )
                return 0
            except Exception:
                # 失败会话保留给 DSH 或调用方排查，不能在异常路径中销毁现场。
                raise
        session = TmuxSession.attach(name=args.session, expected_agent=adapter.kind)
        if args.command == "send":
            _emit(
                submit(
                    session=session,
                    adapter=adapter,
                    task_file=args.task_file,
                    receipt_file=args.receipt,
                    token=args.token,
                    script=script,
                    timeout_seconds=args.timeout_seconds,
                )
            )
        elif args.command == "status":
            _emit({"schema_version": 1, **session.status().to_json()})
        elif args.command == "capture":
            _emit(
                {
                    "schema_version": 1,
                    **session.status().to_json(),
                    "screen": session.capture(history_lines=args.history_lines),
                }
            )
        elif args.command == "close":
            session.close()
            _emit({"schema_version": 1, "status": "closed", "session": args.session})
        return 0
    except (WorkerError, TmuxError, RecordError, OSError) as exc:
        print(f"{adapter.kind}_session: {exc}", file=sys.stderr)
        return 1
