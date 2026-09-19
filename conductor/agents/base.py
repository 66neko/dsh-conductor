"""共享控制器机制；所有 TUI 差异由具体 adapter 负责。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Pattern, Sequence

from ..errors import OperationError
from ..runtime import controller_settings
from ..models import RecordError, WorkerReceipt, read_json_object, read_worker_result, worker_result_path
from ..state import atomic_write_json
from ..tmux import DEFAULT_CAPTURE_HISTORY_LINES, TmuxError, TmuxSession
from ..supervision import SupervisionError, Supervisor


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
    confirm_submission: bool = False
    error_hint: Pattern[str] = re.compile(
        r"connection (?:failed|reset|refused)|network error|request timed out|"
        r"unable to connect|failed to connect|API error|rate limit|overloaded|"
        r"authentication (?:failed|error)|tool not found|工具不可用|连接失败|网络故障|阻塞：",
        re.IGNORECASE,
    )
    busy_hint: Pattern[str] = re.compile(r"esc to interrupt|thinking|思考中", re.IGNORECASE)

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

    def has_pending_submission(self, screen: str, activity_status: str = "") -> bool:
        return self.pending_submission is not None and bool(self.pending_submission.search(screen))

    def submission_status(self, screen: str, activity_status: str) -> str:
        return "unknown"


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
    result_file = worker_result_path(receipt)
    return f"""
<dsh_conductor_handoff>
请完整读取任务文件 `{task_file}`，自主完成其中的任务和自检。该文件不是验收结论。
把完整回答、关键结论、修改的文件、执行的检查及结果、未完成事项写入 UTF-8 文件
`{result_file}`。重要内容随工作及时保存；长输出保存在本轮目录的独立文件中，并在结果文件
中记录路径，不能只留在终端。提交回执前，先写临时文件再原子替换 result.md，确保最终结果完整。
终端只显示简短进度和文件路径。若任务阻塞，也必须先把原因和已完成工作写入结果文件。
全部编辑与检查结束后，最后一个工具操作必须运行下列命令，并把 summary 占位文字改为简短事实总结：

{command}

如果外部阻塞导致无法完成，改用下列命令，并在 summary 中说明阻塞原因：

{blocked}

写入回执后不要继续工作。回执只表示可以交给 DSH 独立验收，不表示任务已经通过验收。
</dsh_conductor_handoff>
""".strip()


def build_parser(adapter: AgentAdapter) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{adapter.kind}_session.py",
        description=f"Control one {adapter.kind} interactive session through tmux.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    for name in ("run", "send"):
        command = commands.add_parser(name, help="register a task; watch submits it when the worker is ready")
        command.add_argument("--request", type=Path, required=True)
        command.add_argument("--session", required=True)
        command.add_argument("--task-file", type=Path, required=True)
        command.add_argument("--receipt", type=Path, required=True)
        command.add_argument("--token", required=True)
    commands.choices["run"].add_argument("--binary")

    for name in ("watch", "recover", "choose", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--request", type=Path, required=True)
        command.add_argument("--session", required=True)
    watch = commands.choices["watch"]
    watch.add_argument("--wait-seconds", type=float, default=300)
    watch.add_argument("--acknowledge", type=int)
    recover = commands.choices["recover"]
    recover.add_argument("--observation", type=int, required=True)
    recover.add_argument("--reason", required=True)
    recover.add_argument("--instruction-file", type=Path)
    recover.add_argument("--interrupt", action="store_true")
    choose = commands.choices["choose"]
    choose.add_argument("--observation", type=int, required=True)
    choose.add_argument("--reason", required=True)
    choose.add_argument("--keys", nargs="+", required=True)
    commands.choices["stop"].add_argument("--reason", required=True)

    for name in ("status", "capture", "close"):
        command = commands.add_parser(name)
        command.add_argument("--session", required=True)
        command.add_argument("--socket", type=Path, default=Path(os.environ["DSH_CONDUCTOR_SOCKET"]) if os.environ.get("DSH_CONDUCTOR_SOCKET") else None)
    capture = commands.choices["capture"]
    capture.add_argument("--history-lines", type=int, default=DEFAULT_CAPTURE_HISTORY_LINES)

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
            read_worker_result(args.receipt)
            if not args.summary.strip():
                raise WorkerError("summary must not be empty")
            atomic_write_json(
                args.receipt.resolve(),
                WorkerReceipt(args.token, args.status, args.summary).to_json(),
            )
            _emit({
                "schema_version": 1,
                "status": "receipt_written",
                "receipt": str(args.receipt.resolve()),
                "result_file": str(worker_result_path(args.receipt).resolve()),
            })
            return 0
        if args.command in {"run", "send", "watch", "recover", "choose", "stop"}:
            supervisor = Supervisor(args.request, adapter, args.session)
            if args.command in {"run", "send"}:
                value = supervisor.start(
                    task_file=args.task_file, receipt_file=args.receipt, token=args.token,
                    submission=_submission_prompt(task_file=args.task_file.resolve(), script=script,
                                                  receipt=args.receipt.resolve(), token=args.token),
                    command=adapter.resolve_command(args.binary) if args.command == "run" else None,
                )
            elif args.command == "watch":
                value = supervisor.watch(wait_seconds=args.wait_seconds, acknowledge=args.acknowledge)
            elif args.command == "recover":
                value = supervisor.recover(observation=args.observation, reason=args.reason,
                                           instruction_file=args.instruction_file, interrupt=args.interrupt)
            elif args.command == "choose":
                value = supervisor.choose(observation=args.observation, keys=args.keys, reason=args.reason)
            else:
                value = supervisor.stop(reason=args.reason)
            _emit(value)
            return 0
        budget = None
        runtime_file = None
        if runtime_path := os.environ.get("DSH_CONDUCTOR_RUNTIME"):
            runtime_file = Path(runtime_path)
            request = read_json_object(runtime_file.parent / "request.json")
            socket_path, budget, runtime_file = controller_settings(request, runtime_file.parent)
            if args.socket != socket_path:
                raise WorkerError("socket does not match the active run")
        session = TmuxSession.attach(name=args.session, expected_agent=adapter.kind, socket_path=args.socket,
                                     budget=budget, runtime_file=runtime_file)
        if args.command == "status":
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
    except (WorkerError, SupervisionError, OperationError, RecordError, OSError, ValueError) as exc:
        print(f"{adapter.kind}_session: {exc}", file=sys.stderr)
        return 1
