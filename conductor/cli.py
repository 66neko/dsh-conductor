#!/usr/bin/env python3
"""dsh-conductor 命令行入口。

子命令：

    run            下达任务给 DSH，由它委派下级 agent 并独立验收（默认子命令）
    install-skill  把仓库内的 skill 软链到 ~/.dsh/skills/，供所有 agent 发现
    doctor         检查环境依赖

`conductor --help` 看完整参数。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .dsh import DshClient, DshConfig, DshError, resolve_dsh_bin
from .progress import AgentFollower, ProgressReporter
from .prompt import build_prompt
from .transcript import AgentTranscript

# 仓库根：conductor/cli.py → conductor/ → 仓库根
REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_NAME = "tmux-coding-agents"
# 编排中间产物放工作目录下的独立子目录，不污染用户文件
STATE_DIRNAME = ".dsh-orchestrator"
DEFAULT_VERIFY = "产物文件确实存在于工作目录，且内容完整、符合任务要求。"


# ---------------------------------------------------------------------------
# skill 路径解析与安装
# ---------------------------------------------------------------------------

def repo_skill_dir() -> Path:
    """仓库内的 skill 源目录（唯一真实副本）。"""
    return REPO_ROOT / "skills" / SKILL_NAME


def installed_skill_dir() -> Path:
    """DSH 扫描的 skill 位置：`$DSH_HOME/skills/<name>`。"""
    home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(home) / "skills" / SKILL_NAME


def resolve_skill_dir(explicit=None) -> Path:
    """定位实际可用的 skill：显式参数 > 已安装位置 > 仓库内。

    @throws DshError - 三处都找不到 `scripts/agent_task.py` 时
    """
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates += [installed_skill_dir(), repo_skill_dir()]
    for candidate in candidates:
        if (candidate / "scripts" / "agent_task.py").exists():
            return candidate
    raise DshError(
        "找不到 tmux-coding-agents skill。请运行 `conductor install-skill`，"
        f"或用 --skill-dir 指定。已尝试：{[str(c) for c in candidates]}"
    )


def cmd_install_skill(args) -> int:
    """把仓库内的 skill 软链到 DSH 的 skill 目录。

    用软链而不是复制：仓库是唯一源，改完立即生效，不会出现两份副本漂移。
    DSH 支持软链 skill——加载时会给出解析后的指令文件路径。
    """
    source = repo_skill_dir()
    if not (source / "SKILL.md").exists():
        print(f"conductor: 仓库内找不到 skill：{source}", file=sys.stderr)
        return 1
    target = installed_skill_dir()
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_symlink():
        if target.resolve() == source.resolve():
            print(f"已是最新（软链已存在）：{target} → {source}")
            return 0
        target.unlink()
    elif target.exists():
        # 是真实目录：备份而不是删除，避免毁掉用户可能改过的副本
        backup = target.with_name(f"{target.name}.bak-{int(time.time())}")
        shutil.move(str(target), str(backup))
        print(f"原目录已备份到 {backup}")

    os.symlink(source, target, target_is_directory=True)
    print(f"已安装：{target} → {source}")
    print("重启会话后，所有 agent 即可在 skill 目录中看到 tmux-coding-agents。")
    return 0


# ---------------------------------------------------------------------------
# 环境自检
# ---------------------------------------------------------------------------

def cmd_doctor(args) -> int:
    """检查运行所需的每一项外部依赖，逐条给出结论和修复建议。"""
    rows = []

    tmux = shutil.which("tmux")
    rows.append(("tmux", tmux is not None, tmux or "未安装：apt install tmux"))

    try:
        dsh_bin = resolve_dsh_bin(args.dsh_bin)
        rows.append(("dsh (DeepSeek Harness)", True, dsh_bin))
    except DshError as exc:
        rows.append(("dsh (DeepSeek Harness)", False, str(exc)))

    try:
        skill = resolve_skill_dir(args.skill_dir)
        installed = skill == installed_skill_dir()
        rows.append((
            f"skill {SKILL_NAME}",
            installed,
            str(skill) if installed else f"{skill}（未安装，运行 conductor install-skill）",
        ))
    except DshError as exc:
        rows.append((f"skill {SKILL_NAME}", False, str(exc)))

    home = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
    credentials = home / ".credentials.yaml"
    rows.append((
        "DSH 凭据",
        credentials.exists() or bool(os.environ.get("DEEPSEEK_API_KEY")),
        str(credentials) if credentials.exists() else "未找到，请登录或设置 DEEPSEEK_API_KEY",
    ))

    for kind in ("claude", "codex"):
        path = shutil.which(kind)
        rows.append((f"下级 agent: {kind}", path is not None,
                     path or "未安装（可选，用哪个由 --agent 决定）"))

    width = max(len(name) for name, _, _ in rows)
    print(f"dsh-conductor {__version__} 环境自检\n")
    for name, ok, detail in rows:
        print(f"  {'✅' if ok else '⚠️ '} {name.ljust(width)}  {detail}")
    print()
    missing = [name for name, ok, _ in rows[:2] if not ok]
    if missing:
        print(f"缺少必需项：{', '.join(missing)}", file=sys.stderr)
        return 1
    print("必需项齐备。")
    return 0


# ---------------------------------------------------------------------------
# run：主工作流
# ---------------------------------------------------------------------------

def cmd_run(args) -> int:
    workspace = os.path.abspath(os.path.expanduser(args.workspace))
    if not os.path.isdir(workspace):
        print(f"conductor: 工作目录不存在：{workspace}", file=sys.stderr)
        return 2
    try:
        skill_dir = resolve_skill_dir(args.skill_dir)
    except DshError as exc:
        print(f"conductor: {exc}", file=sys.stderr)
        return 2

    # 编排中间产物集中放，不污染用户文件
    state_dir = os.path.join(workspace, STATE_DIRNAME)
    os.makedirs(state_dir, exist_ok=True)
    task_file = os.path.join(state_dir, "task.txt")
    result_file = os.path.join(state_dir, "result.json")
    with open(task_file, "w", encoding="utf-8") as handle:
        handle.write(args.task)
    # 清掉上一次的结论：该文件的存在必须代表本次运行产生了它
    if os.path.exists(result_file):
        os.remove(result_file)

    session = f"cc-{int(time.time()) % 100000}"
    prompt = build_prompt(
        workspace=workspace,
        task=args.task,
        verify=args.verify,
        task_file=task_file,
        result_file=result_file,
        skill_dir=str(skill_dir),
        session=session,
        agent_kind=args.agent,
        max_attempts=args.max_attempts,
    )

    print(f"[conductor] 工作目录 : {workspace}")
    print(f"[conductor] 下级 agent: {args.agent}  (tmux 会话 {session})")
    print(f"[conductor] skill     : {skill_dir}")
    print(f"[conductor] 验收标准 : {args.verify}")
    if not args.quiet:
        print("[conductor] 已把任务交给 DSH，下面是实时进度"
              f"（心跳每 {args.heartbeat_s:g}s 一次）...", file=sys.stderr)
        print("-" * 60, file=sys.stderr)

    config = DshConfig(workspace=workspace, model=args.model, dsh_bin=args.dsh_bin)
    reporter = None if args.quiet else ProgressReporter(verbose=args.verbose_progress,
                                                        heartbeat_s=args.heartbeat_s)
    # 内容来源：agent 自己写的转录（完整、结构化）。
    # 抓屏拿不到备用屏幕滚出去的内容，只作可选的"当前画面"。
    transcript_follower = None
    screen_follower = None
    if not args.quiet:
        transcript_follower = AgentTranscript(
            args.agent, workspace,
            interval_s=args.transcript_interval_s,
            show_thinking=args.transcript_thinking,
        )
        if args.follow_screen:
            screen_follower = AgentFollower(session, prefix=args.agent,
                                            interval_s=args.follow_interval_s)
    for follower in (reporter, transcript_follower, screen_follower):
        if follower is not None:
            follower.start()
    try:
        with DshClient(config) as dsh:
            run = dsh.run(prompt, session_id=f"orchestrator-{session}",
                          timeout_ms=args.timeout_ms, on_event=reporter)
    except DshError as exc:
        print(f"conductor: DSH 运行失败：{exc}", file=sys.stderr)
        return 1
    finally:
        for follower in (screen_follower, transcript_follower, reporter):
            if follower is not None:
                follower.stop()

    if not args.quiet:
        print("-" * 60, file=sys.stderr)

    summary = (f"[conductor] DSH 结束：status={run.status} "
               f"用时={run.elapsed_ms}ms 事件={run.event_count}")
    if reporter is not None:
        summary += f" · {reporter.summary()}"
    if transcript_follower is not None:
        summary += f" · {transcript_follower.summary()}"
    print(summary)
    if run.status != "completed":
        print(f"[conductor] DSH 未正常完成：{run.turn_end_reason}", file=sys.stderr)
        if run.stderr_tail:
            print(run.stderr_tail[-800:], file=sys.stderr)

    # 结论以文件为准（事实），不以模型自述为准
    verdict = None
    if os.path.exists(result_file):
        try:
            with open(result_file, encoding="utf-8") as handle:
                verdict = json.load(handle)
        except json.JSONDecodeError as exc:
            print(f"[conductor] result.json 不是合法 JSON：{exc}", file=sys.stderr)

    print("\n" + "=" * 60)
    if verdict is None:
        print("[conductor] 未拿到结构化验收结果（result.json 缺失或非法）")
        print(f"[conductor] DSH 最后回复：\n{run.final_text}")
        return 1

    accepted = str(verdict.get("status", "")).lower() == "accepted"
    print(f"验收结论 : {'✅ ACCEPTED' if accepted else '❌ REJECTED'}")
    print(f"委派轮次 : {verdict.get('attempts')}")
    print(f"产物文件 : {verdict.get('artifacts')}")
    print(f"验收依据 : {verdict.get('verification')}")
    if verdict.get("notes"):
        print(f"备注     : {verdict['notes']}")
    print(f"下级回复 : {verdict.get('claude_last_message')}")
    print("=" * 60)
    print(f"\n[DSH 总结] {run.final_text}")

    return 0 if (accepted and run.status == "completed") else 1


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conductor",
        description="让 DSH（DeepSeek Harness）编排下级编码 agent 并独立验收",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  conductor run --workspace ./proj --task '创建 hello.html' \\\n"
            "      --verify 'hello.html 存在且含 <h1>Hello</h1>'\n"
            "  conductor install-skill\n"
            "  conductor doctor\n\n"
            "DSH = DeepSeek Harness：https://github.com/deepseek-ai/deepseek-harness\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"dsh-conductor {__version__}")
    sub = parser.add_subparsers(dest="command")

    def add_common(sp):
        sp.add_argument("--skill-dir", help="tmux-coding-agents skill 目录（默认自动解析）")
        sp.add_argument("--dsh-bin", help="dsh 入口路径或命令名（默认自动解析）")
        return sp

    run = add_common(sub.add_parser("run", help="下达任务并等待 DSH 验收（默认子命令）"))
    run.add_argument("--workspace", required=True, help="下级 agent 的工作目录")
    run.add_argument("--task", required=True, help="要完成的任务（自然语言）")
    run.add_argument("--verify", default=DEFAULT_VERIFY, help="DSH 的验收标准（越具体越可靠）")
    run.add_argument("--agent", choices=["claude", "codex"], default="claude", help="下级 agent 类型")
    run.add_argument("--max-attempts", type=int, default=2, help="最多委派轮次")
    run.add_argument("--timeout-ms", type=int, default=1_800_000, help="DSH 整体超时")
    run.add_argument("--model", default="deepseek-flash", help="DSH 使用的模型")
    run.add_argument("--quiet", action="store_true", help="关闭实时进度输出")
    run.add_argument("--verbose-progress", action="store_true", help="进度里显示 DSH 的 reasoning")
    run.add_argument("--heartbeat-s", type=float, default=10.0, help="无事件时的心跳间隔秒数")
    run.add_argument("--transcript-interval-s", type=float, default=1.0, help="转录轮询间隔秒数")
    run.add_argument("--transcript-thinking", action="store_true", help="转录里打印 thinking 块")
    run.add_argument("--follow-screen", action="store_true", help="额外直播下级 agent 的 tmux 屏幕")
    run.add_argument("--follow-interval-s", type=float, default=2.0, help="屏幕轮询间隔秒数")
    run.set_defaults(func=cmd_run)

    sub.add_parser("install-skill",
                   help="把仓库内的 skill 软链到 ~/.dsh/skills/"
                   ).set_defaults(func=cmd_install_skill)

    add_common(sub.add_parser("doctor", help="检查环境依赖")).set_defaults(func=cmd_doctor)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # 不带子命令时默认跑 run，这样 `conductor --workspace ... --task ...` 也成立
    if argv and argv[0] not in ("run", "install-skill", "doctor", "-h", "--help", "--version"):
        argv = ["run", *argv]
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except DshError as exc:
        print(f"conductor: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("conductor: 已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
