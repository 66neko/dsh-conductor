#!/usr/bin/env python3
"""agent_task — 用 tmux 驱动 Claude Code / Codex 完成任务的命令行入口。

Claude Code 与 Codex 是对等选项，用哪个由 --agent 显式指定，没有默认值。

几乎每个子命令都只做一步，方便把机器步骤和人工 `attach` 交替进行。

    python3 agent_task.py run    --agent claude --cwd /proj --task "创建 hello.html" --wait-file /proj/hello.html
    python3 agent_task.py run    --agent codex  --cwd /proj --task "创建 hello.html" --wait-file /proj/hello.html
    python3 agent_task.py start  --agent claude --cwd /proj --session cc1
    python3 agent_task.py read   --session cc1
    python3 agent_task.py send   --session cc1 --text "继续"
    python3 agent_task.py settle --session cc1
    python3 agent_task.py list
    python3 agent_task.py close  --session cc1

退出码：0 成功；1 失败（含 agent 报错或等待超时）；2 用法错误。
"""

from __future__ import annotations

import argparse
import sys

import tmux_agent as ta


def _log(line: str) -> None:
    sys.stderr.write(f"{line}\n")


def _agent_for(args) -> ta.TmuxAgent:
    if not args.session:
        raise ta.TmuxError("需要 --session 指定已有会话")
    return ta.TmuxAgent.attach(args.session, settle_ms=args.settle_ms, log=_log if args.verbose else None)


def cmd_run(args) -> int:
    result = ta.run_agent_task(
        kind=args.agent,
        task=args.task,
        cwd=args.cwd,
        session=args.session,
        binary=args.bin,
        extra_args=args.args or None,
        wait_for_file=args.wait_file,
        timeout_ms=args.timeout_ms,
        quiet_ms=args.quiet_ms,
        keep_alive=args.keep,
        log=_log if args.verbose else None,
    )
    sys.stdout.write(result.reply + "\n")
    _log(
        f"--- settled={result.settled} 用时={result.elapsed_ms}ms "
        f"副作用={result.file_appeared} 会话={result.session} 接管: {result.attach}"
    )
    if args.show_screen:
        sys.stdout.write("\n" + result.screen + "\n")
    return 0


def cmd_start(args) -> int:
    agent = ta.start_agent(
        kind=args.agent,
        cwd=args.cwd,
        session=args.session,
        binary=args.bin,
        extra_args=args.args or None,
        log=_log if args.verbose else None,
    )
    sys.stdout.write(f"{agent.name}\n{agent.attach_hint()}\n")
    return 0


def cmd_send(args) -> int:
    agent = _agent_for(args)
    text = args.text
    if text is None and not sys.stdin.isatty():
        text = sys.stdin.read().rstrip("\n")
    if not text:
        raise ta.TmuxError("需要 --text 或从 stdin 读取内容")
    agent.send_text(text, enter=not args.no_enter)
    return 0


def cmd_keys(args) -> int:
    agent = _agent_for(args)
    agent.send_keys(*args.keys)
    return 0


def cmd_read(args) -> int:
    agent = _agent_for(args)
    if args.plain:
        # 去掉空行：pane 底部通常是一堆空白，直接 tail 会取到空白。
        sys.stdout.write("\n".join(agent.screen_lines(scrollback=args.scrollback)) + "\n")
    else:
        sys.stdout.write(agent.read(scrollback=args.scrollback))
    return 0


def cmd_settle(args) -> int:
    agent = _agent_for(args)
    result = agent.wait_for_settle(quiet_ms=args.quiet_ms, timeout_ms=args.timeout_ms)
    sys.stdout.write(f"settled={result.settled} elapsed_ms={result.elapsed_ms}\n")
    if args.print_screen:
        sys.stdout.write(result.text)
    return 0 if result.settled else 1


def cmd_wait(args) -> int:
    agent = _agent_for(args)
    result = agent.wait_for(args.pattern, timeout_ms=args.timeout_ms)
    sys.stdout.write(f"matched={result.matched} elapsed_ms={result.elapsed_ms}\n")
    return 0 if result.matched else 1


def cmd_list(_args) -> int:
    sessions = ta.TmuxAgent.list_sessions()
    if not sessions:
        sys.stdout.write("（没有 tmux 会话）\n")
        return 0
    for s in sessions:
        attached = "已接管" if s["attached"] else "后台"
        sys.stdout.write(f"{s['name']}\t窗口={s['windows']}\t{attached}\n")
    return 0


def cmd_close(args) -> int:
    ta.TmuxAgent.attach(args.session).close()
    sys.stdout.write(f"已关闭 {args.session}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent_task",
        description="用 tmux 驱动 Claude Code / Codex（独立会话、全权限、免确认）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, with_agent=False):
        if with_agent:
            # 必填，没有默认值：claude 与 codex 是对等选项，用哪个由调用方指定。
            sp.add_argument("--agent", choices=sorted(ta.AGENT_SPECS), required=True,
                            help="要驱动的 agent（必填，无默认）")
            sp.add_argument("--bin", help="可执行文件绝对路径（PATH 上找不到时用）")
            sp.add_argument("--args", nargs=argparse.REMAINDER,
                            help="附加启动参数；必须放在命令最后（会吞掉后面所有内容）")
        sp.add_argument("--verbose", action="store_true", help="把 tmux 调用打到 stderr")
        sp.add_argument("--settle-ms", type=int, default=1200, help="默认稳定窗口（毫秒）")
        return sp

    sp = common(sub.add_parser("run", help="一步到位：启动→过弹窗→发任务→等完成"), with_agent=True)
    sp.add_argument("--task", required=True, help="任务文本")
    sp.add_argument("--cwd", help="工作目录（默认当前目录）")
    sp.add_argument("--session", help="tmux 会话名（默认自动生成）")
    sp.add_argument("--wait-file", help="轮询等待该文件出现（事实优于猜屏幕）")
    sp.add_argument("--timeout-ms", type=int, default=300_000)
    sp.add_argument("--quiet-ms", type=int, default=4000)
    sp.add_argument("--keep", action="store_true", help="保留会话供人工 attach 接管")
    sp.add_argument("--show-screen", action="store_true", help="打印最终整屏")
    sp.set_defaults(func=cmd_run)

    sp = common(sub.add_parser("start", help="只启动并过掉弹窗，保留会话"), with_agent=True)
    sp.add_argument("--cwd")
    sp.add_argument("--session")
    sp.set_defaults(func=cmd_start)

    sp = common(sub.add_parser("send", help="向已有会话发送文本"))
    sp.add_argument("--session", required=True)
    sp.add_argument("--text", help="要发送的文本；省略则从 stdin 读")
    sp.add_argument("--no-enter", action="store_true", help="不自动回车")
    sp.set_defaults(func=cmd_send)

    sp = common(sub.add_parser("keys", help="发送命名按键"))
    sp.add_argument("--session", required=True)
    sp.add_argument("keys", nargs="+", help="如 Enter Escape C-c Up Down")
    sp.set_defaults(func=cmd_keys)

    sp = common(sub.add_parser("read", help="抓取当前屏幕"))
    sp.add_argument("--session", required=True)
    sp.add_argument("--scrollback", type=int, help="额外包含 N 行历史")
    sp.add_argument("--plain", action="store_true", help="去掉空行")
    sp.set_defaults(func=cmd_read)

    sp = common(sub.add_parser("settle", help="等待界面稳定"))
    sp.add_argument("--session", required=True)
    sp.add_argument("--quiet-ms", type=int, default=1500)
    sp.add_argument("--timeout-ms", type=int, default=120_000)
    sp.add_argument("--print-screen", action="store_true")
    sp.set_defaults(func=cmd_settle)

    sp = common(sub.add_parser("wait", help="等待屏幕匹配正则"))
    sp.add_argument("--session", required=True)
    sp.add_argument("--pattern", required=True)
    sp.add_argument("--timeout-ms", type=int, default=120_000)
    sp.set_defaults(func=cmd_wait)

    sub.add_parser("list", help="列出 tmux 会话").set_defaults(func=cmd_list)

    sp = sub.add_parser("close", help="关闭会话")
    sp.add_argument("--session", required=True)
    sp.set_defaults(func=cmd_close, verbose=False, settle_ms=1200)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not ta.tmux_available():
        _log("找不到 tmux，请先安装（apt install tmux）")
        return 2
    try:
        return args.func(args)
    except ta.TmuxError as exc:
        _log(f"agent_task: {exc}")
        return 1
    except KeyboardInterrupt:
        _log("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
