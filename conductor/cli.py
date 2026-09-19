"""conductor 命令行入口；任务执行统一委托给 Python SDK。"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .dsh import DshError, probe_dsh
from .models import AgentKind, RecordError, read_json_object
from .progress import RunEvent
from .sdk import Conductor, ConductorConfig, ConductorError
from .skills import (
    dsh_home,
    prepare_workspace_skills,
    source_skill,
)
from .state import default_state_root
from .worker_log import DEFAULT_WORKER_LOG_INTERVAL_SECONDS

type JsonObject = dict[str, Any]


def _json(value: object) -> None:
    # stdout 是机器协议通道，进度和诊断统一由 SDK 事件回调写 stderr。
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _event_to_stderr(event: RunEvent) -> None:
    print(event.format(), file=sys.stderr)


def cmd_install_skills(args: argparse.Namespace) -> int:
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise DshError(f"workspace is not a directory: {workspace}")
    skill_root = prepare_workspace_skills(workspace)
    records: list[JsonObject] = []
    for kind in AgentKind:
        source = source_skill(kind)
        target = skill_root / kind.skill_name
        record: JsonObject = {
            "name": kind.skill_name,
            "status": "overwritten",
            "source": str(source),
            "target": str(target),
        }
        records.append(record)
    _json(
        {
            "schema_version": 1,
            "status": "ok",
            "workspace": str(workspace),
            "skill_dir": str(skill_root),
            "skills": records,
        }
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    home = dsh_home(args.dsh_home)
    checks: list[JsonObject] = []

    def add(name: str, ok: bool, detail: object, *, required: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "required": required, "detail": str(detail)})

    add("python", sys.version_info >= (3, 13), sys.version.split()[0])
    tmux = shutil.which("tmux")
    add("tmux", tmux is not None, tmux or "not found")
    try:
        add("dsh", True, probe_dsh(args.dsh_bin))
    except DshError as exc:
        add("dsh", False, exc)
    available_workers: list[str] = []
    for kind in AgentKind:
        try:
            add(kind.skill_name, True, source_skill(kind))
        except DshError as exc:
            add(kind.skill_name, False, exc)
        binary = shutil.which(kind.value)
        add(f"worker:{kind.value}", binary is not None, binary or "not found", required=False)
        if binary:
            available_workers.append(kind.value)
    add("worker:any", bool(available_workers), ", ".join(available_workers) or "neither claude nor codex found")
    credentials = home / ".credentials.yaml"
    authenticated = credentials.is_file() or bool(os.environ.get("DEEPSEEK_API_KEY"))
    add("dsh-credentials", authenticated, credentials if credentials.is_file() else "not found")
    ok = all(check["ok"] for check in checks if check["required"])
    _json({"schema_version": 1, "status": "ok" if ok else "error", "checks": checks})
    return 0 if ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    workspace = args.workspace.expanduser().resolve()
    try:
        config = ConductorConfig(
            dsh_bin=args.dsh_bin,
            dsh_home=args.dsh_home,
            state_dir=args.state_dir,
            provider=args.provider,
            model=args.model,
            max_attempts=args.max_attempts,
            worker_idle_timeout_seconds=args.worker_idle_timeout_seconds,
            max_recovery_attempts=args.max_recovery_attempts,
            sdk_heartbeat_counts_as_activity=args.sdk_heartbeat_counts_as_activity,
            timeout_seconds=args.timeout_seconds,
            keep_session=args.keep_session,
            heartbeat_seconds=args.heartbeat_seconds,
            worker_log=args.worker_log,
            worker_log_interval_seconds=args.worker_log_interval_seconds,
        )
        result = Conductor(workspace, config).run(
            args.prompt,
            on_event=None if args.quiet else _event_to_stderr,
        )
    except (ConductorError, ValueError, OSError) as exc:
        if isinstance(exc, ConductorError):
            _json(exc.to_json())
        else:
            _json({"schema_version": 1, "status": "error", "error": str(exc)})
        return 1
    _json(result.to_json())
    return 0 if result.accepted else 1


def cmd_show(args: argparse.Namespace) -> int:
    if args.state_dir is not None:
        state_root = args.state_dir.expanduser().resolve()
    elif args.workspace is not None:
        state_root = default_state_root(args.workspace)
    else:
        _json(
            {
                "schema_version": 1,
                "status": "error",
                "error": "show requires --workspace unless --state-dir is provided",
            }
        )
        return 1
    if args.run_id:
        run_root = state_root / "runs" / args.run_id
    else:
        try:
            latest = read_json_object(state_root / "latest.json")
            run_root = Path(str(latest["run_directory"]))
        except (RecordError, KeyError) as exc:
            _json({"schema_version": 1, "status": "error", "error": f"cannot resolve latest run: {exc}"})
            return 1
    try:
        request = read_json_object(run_root / "request.json")
    except RecordError as exc:
        _json({"schema_version": 1, "status": "error", "error": str(exc)})
        return 1
    output: JsonObject = {"schema_version": 1, "run_directory": str(run_root), "request": request}
    for name in ("user-prompt.md", "manager-prompt.md", "plan.json", "verdict.json", "supervision.json", "supervision.jsonl", "worker-screen.log"):
        path = run_root / name
        if not path.exists():
            continue
        if path.suffix == ".json":
            try:
                output[path.stem] = read_json_object(path)
            except RecordError as exc:
                output[f"{path.stem}_error"] = str(exc)
        elif path.suffix == ".jsonl":
            output["supervision_log"] = str(path)
        elif path.suffix == ".log":
            output["worker_log"] = str(path)
        else:
            output[path.stem.replace("-", "_")] = path.read_text(encoding="utf-8")
    _json(output)
    return 0


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_empty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="conductor", description="通过 DSH 管理并验收编码任务。")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="运行一个包含任务与验收标准的 prompt")
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--prompt", type=_non_empty, required=True)
    run.add_argument("--max-attempts", type=_positive, default=2)
    run.add_argument("--worker-idle-timeout-seconds", type=_positive, default=300)
    run.add_argument("--sdk-heartbeat-counts-as-activity", action=argparse.BooleanOptionalAction, default=True,
                     help="SDK 心跳计入活动（默认开启）；持续心跳会阻止静默超时")
    run.add_argument("--max-recovery-attempts", type=_positive, default=5)
    run.add_argument("--timeout-seconds", type=_positive_float, default=3600.0)
    run.add_argument("--provider", default="deepseek-official")
    run.add_argument("--model", default="deepseek-flash")
    run.add_argument("--dsh-bin")
    run.add_argument("--dsh-home", type=Path)
    run.add_argument("--state-dir", type=Path)
    run.add_argument("--keep-session", action="store_true")
    run.add_argument("--quiet", action="store_true")
    run.add_argument("--heartbeat-seconds", type=_positive_float, default=10.0)
    run.add_argument("--worker-log", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument(
        "--worker-log-interval-seconds",
        type=_positive_float,
        default=DEFAULT_WORKER_LOG_INTERVAL_SECONDS,
        help="tmux worker 屏幕采样间隔，默认 10 秒；结束时立即补采",
    )
    run.set_defaults(func=cmd_run)
    install = commands.add_parser("install-skills", help="将两个 skill 复制到指定 workspace")
    install.add_argument("--workspace", type=Path, required=True)
    install.set_defaults(func=cmd_install_skills)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--dsh-bin")
    doctor.add_argument("--dsh-home", type=Path)
    doctor.set_defaults(func=cmd_doctor)
    show = commands.add_parser("show")
    show.add_argument("--run-id")
    show.add_argument("--workspace", type=Path)
    show.add_argument("--state-dir", type=Path)
    show.set_defaults(func=cmd_show)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _json({"schema_version": 1, "status": "error", "error": "interrupted"})
        return 130
    except (DshError, OSError) as exc:
        _json({"schema_version": 1, "status": "error", "error": str(exc)})
        return 1
