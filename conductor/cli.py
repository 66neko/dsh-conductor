"""DSH 编码任务的命令行边界。"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .dsh import DshClient, DshConfig, DshError, probe_dsh
from .models import AgentKind, RecordError, Verdict, read_json_object
from .progress import ProgressReporter
from .prompt import build_prompt
from .state import RunState, default_state_root

type JsonObject = dict[str, Any]

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_NAMES = tuple(kind.skill_name for kind in AgentKind)


def _json(value: object) -> None:
    # stdout 是调用方读取的协议通道，任何进度或诊断都不能写到这里。
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _dsh_home(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    return Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")).expanduser().resolve()


def _source_skill(kind: AgentKind) -> Path:
    root = Path(os.environ.get("DSH_CONDUCTOR_SKILLS", REPO_ROOT / "skills"))
    path = root.expanduser().resolve() / kind.skill_name
    if not (path / "SKILL.md").is_file():
        raise DshError(f"bundled skill is missing: {path}")
    return path


def _installed_skill(kind: AgentKind, dsh_home: Path) -> Path:
    path = dsh_home / "skills" / kind.skill_name
    script = path / "scripts" / kind.script_name
    if not path.is_symlink() or not script.is_file():
        raise DshError(
            f"{kind.skill_name} is not installed as a valid symlink; run `conductor install-skills`"
        )
    return path.resolve()


def _install_link(source: Path, target: Path) -> tuple[str, str | None]:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() and target.resolve(strict=False) == source:
        return "unchanged", None
    backup: str | None = None
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        # 真实目录可能包含用户内容，先备份再建立唯一来源的软链。
        backup_path = target.with_name(f"{target.name}.backup-{int(time.time())}")
        shutil.move(target, backup_path)
        backup = str(backup_path)
    target.symlink_to(source, target_is_directory=True)
    return "installed", backup


def cmd_install_skills(args: argparse.Namespace) -> int:
    dsh_home = _dsh_home(args.dsh_home)
    records: list[JsonObject] = []
    for kind in AgentKind:
        source = _source_skill(kind)
        target = dsh_home / "skills" / kind.skill_name
        status, backup = _install_link(source, target)
        record: JsonObject = {
            "name": kind.skill_name,
            "status": status,
            "source": str(source),
            "target": str(target),
        }
        if backup:
            record["backup"] = backup
        records.append(record)
    legacy = dsh_home / "skills" / "tmux-coding-agents"
    if legacy.is_symlink() and not legacy.exists():
        legacy.unlink()
    _json({"schema_version": 1, "status": "ok", "skills": records})
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    dsh_home = _dsh_home(args.dsh_home)
    checks: list[JsonObject] = []

    def add(name: str, ok: bool, detail: object, *, required: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "required": required, "detail": str(detail)})

    add("python", sys.version_info >= (3, 13), sys.version.split()[0])
    add("tmux", bool(path := shutil.which("tmux")), path or "not found")
    try:
        add("dsh", True, probe_dsh(args.dsh_bin))
    except DshError as exc:
        add("dsh", False, exc)
    worker_binaries: dict[AgentKind, str | None] = {}
    for kind in AgentKind:
        try:
            add(kind.skill_name, True, _installed_skill(kind, dsh_home))
        except DshError as exc:
            add(kind.skill_name, False, exc)
        binary = shutil.which(kind.value)
        worker_binaries[kind] = binary
        add(f"worker:{kind.value}", binary is not None, binary or "not found", required=False)
    available_workers = [kind.value for kind, binary in worker_binaries.items() if binary]
    add(
        "worker:any",
        bool(available_workers),
        ", ".join(available_workers) if available_workers else "neither claude nor codex found",
    )
    credentials = dsh_home / ".credentials.yaml"
    authenticated = credentials.is_file() or bool(os.environ.get("DEEPSEEK_API_KEY"))
    add("dsh-credentials", authenticated, credentials if credentials.is_file() else "not found")
    ok = all(check["ok"] for check in checks if check["required"])
    _json({"schema_version": 1, "status": "ok" if ok else "error", "checks": checks})
    return 0 if ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        _json({"schema_version": 1, "status": "error", "error": f"workspace is not a directory: {workspace}"})
        return 2
    agent = AgentKind(args.agent)
    dsh_home = _dsh_home(args.dsh_home)
    try:
        skill = _installed_skill(agent, dsh_home)
    except DshError as exc:
        _json({"schema_version": 1, "status": "error", "error": str(exc)})
        return 2
    state = RunState.create(
        state_root=args.state_dir,
        workspace=workspace,
        agent=agent,
        task=args.task,
        acceptance=args.verify,
        max_attempts=args.max_attempts,
        attempt_timeout_seconds=args.attempt_timeout_seconds,
        keep_session=args.keep_session,
    )
    skill_script = skill / "scripts" / agent.script_name
    prompt = build_prompt(
        state,
        skill_script=skill_script,
        max_attempts=args.max_attempts,
        attempt_timeout_seconds=args.attempt_timeout_seconds,
        keep_session=args.keep_session,
    )
    state.write_prompt(prompt)
    if not args.quiet:
        print(f"conductor: run {state.run_id}", file=sys.stderr)
        print(f"conductor: worker {agent.value} in tmux session {state.session}", file=sys.stderr)
        print(f"conductor: state {state.root}", file=sys.stderr)
    reporter = None if args.quiet else ProgressReporter(heartbeat_seconds=args.heartbeat_seconds).start()
    run = None
    try:
        config = DshConfig(
            workspace=workspace,
            dsh_bin=args.dsh_bin,
            provider=args.provider,
            model=args.model,
            dsh_home=dsh_home,
        )
        with DshClient(config) as client:
            run = client.run(
                prompt,
                session_id=f"conductor-{state.run_id}",
                timeout_seconds=args.timeout_seconds,
                on_event=reporter,
            )
    except DshError as exc:
        _json(state.error_output(f"DSH failed: {exc}"))
        return 1
    finally:
        if reporter is not None:
            reporter.stop()

    assert run is not None
    if run.status != "completed":
        message = f"DSH turn ended with status {run.status}"
        if run.stderr_tail:
            message += f": {run.stderr_tail[-1200:]}"
        output = state.error_output(message, dsh_status=run.status)
        if state.verdict_file.exists():
            try:
                output["untrusted_verdict"] = read_json_object(state.verdict_file)
            except RecordError:
                pass
        _json(output)
        return 1
    try:
        # verdict 只是一份外部模型写入的声明，必须再次绑定本轮身份和 receipt 事实。
        verdict = Verdict.load(
            state.verdict_file,
            expected_run_id=state.run_id,
            expected_agent=agent,
            max_attempts=args.max_attempts,
            workspace=workspace,
            expected_receipts=tuple(
                (attempt.receipt_file, attempt.token) for attempt in state.attempts
            ),
        )
    except RecordError as exc:
        _json(state.error_output(f"invalid or missing DSH verdict: {exc}", dsh_status=run.status))
        return 1
    output = verdict.to_json()
    output.update(
        {
            "workspace": str(workspace),
            "state_directory": str(state.root),
            "session": state.session,
            "dsh": {
                "status": run.status,
                "elapsed_seconds": round(run.elapsed_seconds, 3),
                "event_count": len(run.events),
            },
        }
    )
    _json(output)
    return 0 if verdict.status == "accepted" else 1


def cmd_show(args: argparse.Namespace) -> int:
    state_root = args.state_dir.expanduser().resolve()
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
    verdict_path = run_root / "verdict.json"
    if verdict_path.exists():
        try:
            output["verdict"] = read_json_object(verdict_path)
        except RecordError as exc:
            output["verdict_error"] = str(exc)
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
    parser = argparse.ArgumentParser(
        prog="conductor",
        description="Delegate coding work through DSH and accept it only after independent verification.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run")
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--task", type=_non_empty, required=True)
    run.add_argument(
        "--verify",
        type=_non_empty,
        required=True,
        help="explicit acceptance criteria",
    )
    run.add_argument("--agent", choices=[kind.value for kind in AgentKind], required=True)
    run.add_argument("--max-attempts", type=_positive, default=2)
    run.add_argument("--attempt-timeout-seconds", type=_positive, default=1200)
    run.add_argument("--timeout-seconds", type=_positive, default=3600)
    run.add_argument("--provider", default="deepseek-official")
    run.add_argument("--model", default="deepseek-flash")
    run.add_argument("--dsh-bin")
    run.add_argument("--dsh-home", type=Path)
    run.add_argument("--state-dir", type=Path, default=default_state_root())
    run.add_argument("--keep-session", action="store_true")
    run.add_argument("--quiet", action="store_true")
    run.add_argument("--heartbeat-seconds", type=_positive_float, default=10.0)
    run.set_defaults(func=cmd_run)

    install = commands.add_parser("install-skills")
    install.add_argument("--dsh-home", type=Path)
    install.set_defaults(func=cmd_install_skills)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--dsh-bin")
    doctor.add_argument("--dsh-home", type=Path)
    doctor.set_defaults(func=cmd_doctor)

    show = commands.add_parser("show")
    show.add_argument("--run-id")
    show.add_argument("--state-dir", type=Path, default=default_state_root())
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
