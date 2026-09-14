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


def _submission_prompt(*, task_file: Path, script: Path, receipt: Path, token: str) -> str:
    # TUI 只接收短指令；任务正文留在文件中，避免长粘贴在交互式编辑器中被截断。
    command = (
        f"python3.13 {shlex.quote(str(script))} complete "
        f"--receipt {shlex.quote(str(receipt))} --token {shlex.quote(token)} "
        "--status ready_for_verification --summary '简短事实总结'"
    )
    blocked = command.replace("ready_for_verification", "blocked")
    return f"""
<dsh_conductor_handoff>
请完整读取任务文件 `{task_file}`，自主完成其中的任务和自检。该文件不是验收结论。
全部编辑与检查结束后，最后一个工具操作必须运行下列命令，并把 summary 占位文字改为简短事实总结：

{command}

如果外部阻塞导致无法完成，改用下列命令，并在 summary 中说明阻塞原因：

{blocked}

写入回执后不要继续工作。回执只表示可以交给 DSH 独立验收，不表示任务已经通过验收。
</dsh_conductor_handoff>
""".strip()


def wait_until_ready(
    session: TmuxSession,
    adapter: AgentAdapter,
    *,
    timeout_seconds: float,
    ready_settle_seconds: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    menu_steps = 0
    ready_since: float | None = None
    while time.monotonic() < deadline:
        screen = session.capture()
        status = session.status()
        if status.pane_dead:
            raise WorkerError(f"{adapter.kind} exited during startup\n{screen[-2000:]}")
        if adapter.is_menu(screen):
            ready_since = None
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
            # 启动界面可能先显示输入框，随后才弹出目录信任菜单；稳定后再粘贴任务。
            if ready_since is None:
                ready_since = time.monotonic()
            elif time.monotonic() - ready_since >= ready_settle_seconds:
                return
        else:
            ready_since = None
        time.sleep(0.5)
    raise WorkerError(f"{adapter.kind} did not become ready\n{session.capture()[-2000:]}")


def wait_for_receipt(
    session: TmuxSession,
    *,
    adapter: AgentAdapter,
    receipt_file: Path,
    token: str,
    timeout_seconds: float,
    submission: str,
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
            # 菜单可能截断首次粘贴；清空残留输入后完整重投，不能只补发 Enter。
            session.send_keys("C-c")
            time.sleep(0.25)
            session.send_text(submission)
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
    if not task.strip():
        raise WorkerError(f"task file is empty: {task_file}")
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt = _submission_prompt(
        task_file=task_file.resolve(),
        script=script,
        receipt=receipt_file.resolve(),
        token=token,
    )
    session.send_text(prompt)
    # 长文本粘贴可能在首个 Enter 后才完成展开；菜单由后续分支处理，其余状态只补交一次。
    time.sleep(2.0)
    if not receipt_file.exists() and not adapter.is_menu(session.capture()):
        session.send_keys("Enter")
    receipt, elapsed = wait_for_receipt(
        session,
        adapter=adapter,
        receipt_file=receipt_file,
        token=token,
        timeout_seconds=timeout_seconds,
        submission=prompt,
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
