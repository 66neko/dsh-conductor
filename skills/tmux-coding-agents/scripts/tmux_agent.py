#!/usr/bin/env python3
"""tmux 驱动的交互式 CLI 控制器。

在独立的 tmux 会话里启动全屏 TUI（Claude Code / Codex / 任何交互式程序），
用 send-keys 当键盘、capture-pane 当屏幕，同时保留会话可人工接管。

为什么用 tmux：
  1. capture-pane 等于内置一个终端模拟器——直接给你**渲染后的屏幕文本**，
     不用自己解析 ANSI 转义序列（自建 PTY 要自己实现这一层）。
  2. 会话独立于调用进程存活，人工随时 `tmux attach` 接管同一个活动会话。

只用标准库，无第三方依赖。

用法（作为库）：
    from tmux_agent import TmuxAgent, start_agent, submit_task
    agent = start_agent("claude", cwd="/path/to/proj", session="cc-1")
    result = submit_task(agent, "创建一个 hello.html", wait_for_file="/path/to/proj/hello.html")
    print(result.reply)
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

TMUX = "tmux"


class TmuxError(RuntimeError):
    """tmux 调用失败或会话状态异常。"""


# ---------------------------------------------------------------------------
# 底层 tmux 调用
# ---------------------------------------------------------------------------

def _run(args: Sequence[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    """执行一条 tmux 命令。

    始终用 argv 形式而不是拼 shell 字符串：所有文本作为一个独立参数传递，
    不经过 shell 解释，避免引号地狱和注入。
    """
    try:
        return subprocess.run([TMUX, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise TmuxError("未找到 tmux，请先安装（apt install tmux）") from exc
    except subprocess.TimeoutExpired as exc:
        raise TmuxError(f"tmux 命令超时：tmux {' '.join(args)}") from exc


def _run_ok(args: Sequence[str], timeout: float = 20.0) -> str:
    """执行 tmux 命令并要求成功，返回 stdout。"""
    proc = _run(args, timeout=timeout)
    if proc.returncode != 0:
        raise TmuxError(f"tmux {' '.join(args)} 失败：{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def tmux_available() -> bool:
    """tmux 是否可用。"""
    return shutil.which(TMUX) is not None


def safe_session_name(name: str) -> str:
    """tmux 会话名不能含 `.` `:` 和空白。"""
    cleaned = re.sub(r"[.:\s]+", "-", str(name))
    cleaned = re.sub(r"[^\w@%+=,/-]", "", cleaned)
    if not cleaned:
        raise TmuxError("会话名清洗后为空")
    return cleaned


# ---------------------------------------------------------------------------
# 屏幕稳定性判定
# ---------------------------------------------------------------------------

_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07]*\x07")
_DIGITS = re.compile(r"[0-9]+")
# 盲文点阵与星号类 spinner 帧
_SPINNER = re.compile(r"[\u2801-\u28ff\u28fe\u28fd\u28fb\u28bf\u283f\u28df\u28ef\u28f7\u28e7\u2827\u2837\u28b7\u2887\u2897\u28a7\u28c7\u28d7\u282f\u283d\u28fc\u28f8\u28e0\u2804]|[\u2732\u2733\u2736\u273b\u273d\u00b7\u2217*]")


def stability_key(text: str) -> str:
    """把屏幕快照归一化成"稳定键"，用于判断界面是否还在有意义地变化。

    工作中的 TUI 一直在动：spinner 旋转帧、已用时间、token 计数、进度条。
    直接比较原始文本永远不会稳定，所以先把易变部分遮掉再比：数字→`#`，
    spinner 帧→`~`，再剥掉 ANSI。比较的是**结构变化**。

    这是故意有损的：如果任务唯一的进度表现就是变化的数字，会被误判为已完成。
    所以能用 wait_for(正则) 时不要用 wait_for_settle。
    """
    out = _ANSI_CSI.sub("", str(text))
    out = _ANSI_OSC.sub("", out)
    out = _DIGITS.sub("#", out)
    out = _SPINNER.sub("~", out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"[ \t]+$", "", out, flags=re.MULTILINE)
    return out.strip()


@dataclass
class SettleResult:
    """wait_for_settle / wait_for 的结果。"""
    ok: bool
    elapsed_ms: int
    text: str

    @property
    def settled(self) -> bool:
        return self.ok

    @property
    def matched(self) -> bool:
        return self.ok


@dataclass
class TaskResult:
    """submit_task 的结果。"""
    reply: str
    screen: str
    settled: bool
    elapsed_ms: int
    file_appeared: bool
    session: str
    attach: str


# ---------------------------------------------------------------------------
# 会话对象
# ---------------------------------------------------------------------------

class TmuxAgent:
    """一个被程序驱动的 tmux 会话。

    典型用法是把命令**敲进**一个交互式 shell（而不是用 tmux 的 shell-command
    参数启动），这样所有引号由我们以字面量方式控制，而且程序退出后 shell 还在，
    现场仍然可查看、可接管。
    """

    def __init__(
        self,
        name: str,
        cwd: Optional[str] = None,
        width: int = 200,
        height: int = 50,
        settle_ms: int = 1200,
        log: Optional[Callable[[str], None]] = None,
        path_env: Optional[str] = None,
    ) -> None:
        self.name = safe_session_name(name)
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.width = width
        self.height = height
        self.settle_ms = settle_ms
        self.log = log or (lambda _msg: None)
        # tmux 新 pane 继承的是 tmux **服务端**的环境，不是当前进程的。
        # 服务端可能是很早以前用另一份 PATH 启动的，导致 pane 里找不到 node。
        # 显式把当前 PATH 注入 pane（tmux >= 3.0 的 -e）。
        self.path_env = path_env if path_env is not None else os.environ.get("PATH", "")
        self.opened = False

    # -- 生命周期 ---------------------------------------------------------

    def open(self) -> "TmuxAgent":
        """创建一个 detached tmux 会话（默认起一个登录 shell）。"""
        if self.opened:
            return self
        if TmuxAgent.has_session(self.name):
            raise TmuxError(f"会话 {self.name!r} 已存在；请先 close 或换个名字")
        if not os.path.isdir(self.cwd):
            raise TmuxError(f"工作目录不存在：{self.cwd}")

        args = [
            "new-session", "-d",
            "-s", self.name,
            # detached 会话默认 80x24，全屏 TUI 会按这个尺寸重排、界面挤烂，
            # readiness 判断也会失准。必须显式指定几何尺寸。
            "-x", str(self.width),
            "-y", str(self.height),
            "-c", self.cwd,
        ]
        if self.path_env:
            args += ["-e", f"PATH={self.path_env}"]
        self.log(f"tmux {' '.join(args)}")
        _run_ok(args)
        self.opened = True
        return self

    def configure_pane(self, history_limit: int = 50000, mouse: bool = True) -> None:
        """设置滚动历史和鼠标——TUI 通常需要。"""
        _run(["set-option", "-t", self.name, "history-limit", str(history_limit)])
        if mouse:
            _run(["set-option", "-t", self.name, "mouse", "on"])

    def close(self) -> None:
        """结束会话及其中的一切。"""
        if not self.opened:
            if not TmuxAgent.has_session(self.name):
                return
            # 会话存在但我们没记录 opened（例如由 attach 之外的方式构造），
            # 也要真的关掉，而不是静默 no-op。
        _run(["kill-session", "-t", self.name])
        self.opened = False

    def attach_hint(self) -> str:
        """人工接管这个活动会话的命令。"""
        return f"tmux attach -t {self.name}"

    def __enter__(self) -> "TmuxAgent":
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- 读写 -------------------------------------------------------------

    def read(self, scrollback: Optional[int] = None, keep_escapes: bool = False) -> str:
        """抓取当前可见屏幕的文本。"""
        args = ["capture-pane", "-p", "-t", self.name]
        if scrollback is not None:
            args += ["-S", f"-{int(scrollback)}"]
        if keep_escapes:
            args += ["-e"]
        else:
            # -J 把终端软换行接回去，否则逻辑行被拆成多行，解析全乱。
            args += ["-J"]
        return _run_ok(args, timeout=30.0)

    def screen_lines(self, scrollback: Optional[int] = None) -> list[str]:
        """抓屏并去掉空行——pane 底部通常是一堆空行，直接 tail 会取到空白。"""
        return [line for line in self.read(scrollback=scrollback).splitlines() if line.strip()]

    def send_text(self, text: str, enter: bool = False, enter_delay_ms: int = 120) -> None:
        """把文本按**字面量**敲进 pane，可选随后回车。

        `-l` 是关键：不加的话 tmux 会把 `Enter`、`C-c`、`Up` 这些词当成**按键名**。
        如果 prompt 里出现 "Enter" 这个词，就会被解释成回车。

        `enter_delay_ms` 是文本与回车之间的短暂停顿：TUI 收到输入会重绘输入框，
        紧接着到达的回车可能被吞掉（实测 Codex 偶发）。设为 0 可关闭。
        """
        _run_ok(["send-keys", "-t", self.name, "-l", "--", text])
        if enter:
            if enter_delay_ms > 0:
                time.sleep(enter_delay_ms / 1000)
            self.send_keys("Enter")

    def send_keys(self, *keys: str) -> None:
        """发送命名按键（不是字面文本）：Enter、Escape、C-c、Up、BTab……"""
        if not keys:
            return
        _run_ok(["send-keys", "-t", self.name, *keys])

    # -- 等待 -------------------------------------------------------------

    def wait_for_settle(
        self,
        quiet_ms: Optional[int] = None,
        timeout_ms: int = 120_000,
        poll_ms: int = 200,
        min_wait_ms: int = 0,
    ) -> SettleResult:
        """等到界面不再有意义地变化（启发式，见 stability_key）。"""
        quiet = self.settle_ms if quiet_ms is None else quiet_ms
        started = time.monotonic()
        previous: Optional[str] = None
        last_change = started
        text = ""

        while True:
            text = self.read()
            key = stability_key(text)
            now = time.monotonic()
            if key != previous:
                previous = key
                last_change = now
            elapsed_ms = int((now - started) * 1000)
            if elapsed_ms >= min_wait_ms and (now - last_change) * 1000 >= quiet:
                return SettleResult(True, elapsed_ms, text)
            if elapsed_ms >= timeout_ms:
                return SettleResult(False, elapsed_ms, text)
            time.sleep(poll_ms / 1000)

    def wait_for(self, pattern: "re.Pattern[str] | str", timeout_ms: int = 120_000, poll_ms: int = 200) -> SettleResult:
        """等到屏幕匹配某个模式。

        能锚定到真实文本时优先用它——那是**事实**，不是启发式。
        """
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        started = time.monotonic()
        text = ""
        while True:
            text = self.read()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if rx.search(text):
                return SettleResult(True, elapsed_ms, text)
            if elapsed_ms >= timeout_ms:
                return SettleResult(False, elapsed_ms, text)
            time.sleep(poll_ms / 1000)

    def ask(self, text: str, enter: bool = True, **settle_kwargs) -> SettleResult:
        """一次拟人交互：敲文本、回车、等界面稳定。"""
        self.send_text(text, enter=enter)
        settle_kwargs.setdefault("min_wait_ms", 400)
        return self.wait_for_settle(**settle_kwargs)

    # -- 静态查询 ---------------------------------------------------------

    @staticmethod
    def has_session(name: str) -> bool:
        proc = _run(["has-session", "-t", safe_session_name(name)])
        return proc.returncode == 0

    @classmethod
    def attach(cls, name: str, settle_ms: int = 1200, log: Optional[Callable[[str], None]] = None) -> "TmuxAgent":
        """接管一个**已经存在**的 tmux 会话。

        用于会话由人工或其他程序创建的场景（例如你 `tmux new-session` 起好之后
        再让脚本接管）。不要用构造函数去接既有会话——那会需要手动改私有状态。
        """
        safe = safe_session_name(name)
        if not cls.has_session(safe):
            raise TmuxError(f"会话 {safe!r} 不存在（用 list 查看）")
        cwd = _run_ok(["display-message", "-p", "-t", safe, "#{pane_current_path}"]).strip() or os.getcwd()
        agent = cls(name=safe, cwd=cwd, settle_ms=settle_ms, log=log)
        agent.opened = True
        return agent

    @staticmethod
    def list_sessions() -> list[dict]:
        proc = _run(["list-sessions", "-F", "#{session_name}\t#{session_windows}\t#{session_attached}"])
        if proc.returncode != 0:
            return []  # 没有 tmux 服务端不算错误
        out = []
        for line in proc.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            out.append({
                "name": parts[0],
                "windows": int(parts[1]) if len(parts) > 1 else 0,
                "attached": len(parts) > 2 and parts[2] == "1",
            })
        return out


# ---------------------------------------------------------------------------
# 启动弹窗处理
# ---------------------------------------------------------------------------

# 菜单/选择光标可能使用的符号。各 agent 不同：
#   Claude Code 用 ❯ (U+276F)，Codex 用 › (U+203A)。
# 刻意**不包含**裸露的 `>`：Codex 的欢迎语里有 "> You are in /tmp"、
# 标题框里有 ">_" 等文本，会把普通文字误判成光标。
_CURSOR_GLYPHS = "❯›»▸▶→"

# 菜单的确认提示语。同样因 agent 而异：
#   Claude Code: "Enter to confirm · Esc to cancel"
#   Codex:       "Press enter to continue"
_MENU_HINT = re.compile(
    r"Enter to confirm|Esc to cancel|Press enter to continue|↑/↓|to navigate|to select",
    re.IGNORECASE,
)


def is_menu(screen: str) -> bool:
    """当前屏幕是不是一个需要应答的菜单。

    不能只看光标符号：**菜单光标和聊天输入框用的是同一个符号**
    （Claude Code 都是 `❯`，Codex 都是 `›`）。可靠判别是确认提示语——
    菜单有，输入框永远没有。
    """
    return bool(_MENU_HINT.search(screen))


def cursor_label(screen: str) -> Optional[str]:
    """菜单光标当前所在选项的文案（已去掉 `1.` 之类的序号前缀）。

    Codex 的选项形如 `› 1. Yes, continue`，序号会让 `^Yes` 这类匹配失效，
    所以这里统一剥掉前导编号。
    """
    for line in str(screen).splitlines():
        for glyph in _CURSOR_GLYPHS:
            if glyph in line:
                label = line.split(glyph, 1)[1].strip()
                return re.sub(r"^\d+[.)]\s*", "", label)
    return None


def accept_menu_option(
    agent: TmuxAgent,
    accept: "re.Pattern[str]",
    max_steps: int = 8,
    step_ms: int = 250,
    log: Optional[Callable[[str], None]] = None,
) -> tuple[bool, Optional[str]]:
    """驱动菜单，直到高亮项匹配 `accept`，然后回车。

    做法是**逐格移动光标并重新读屏**，而不是"解析选项列表再算 Down 次数"：
    解析得猜选项从哪行开始到哪行结束，而弹窗装饰文本（"Security guide"、URL 行）
    缩进和选项一样，无法靠缩进区分。重新读屏是精确的，而且天然容忍菜单循环。

    返回 (是否处理, 选中的文案)。
    """
    log = log or (lambda _m: None)
    for _ in range(max_steps + 1):
        screen = agent.read()
        if not is_menu(screen):
            return False, None
        label = cursor_label(screen)
        if label is not None and accept.search(label):
            log(f"确认菜单项：{label!r}")
            agent.send_keys("Enter")
            time.sleep(step_ms / 1000 * 4)  # 等弹窗消失再看
            return True, label
        log(f"光标在 {label!r}，下移")
        # 越界时 tmux/TUI 通常会停住或回绕，有界遍历足以覆盖全部选项。
        agent.send_keys("Down")
        time.sleep(step_ms / 1000)
    return False, None


# 各 agent 的对等启动配置。两者地位相同，用哪个由调用方指定
# （CLI 的 --agent，或库的 start_agent(kind)），本文件不替调用方做选择。
# 新增 agent：在这里加一项即可（bin + args）。
AGENT_SPECS: dict[str, dict] = {
    "claude": {
        "bin": "claude",
        "args": ["--dangerously-skip-permissions"],
        "verified": "已实测（Claude Code 2.1.270）",
    },
    "codex": {
        "bin": "codex",
        "args": ["--dangerously-bypass-approvals-and-sandbox"],
        "verified": "参数取自官方文档；开发本 skill 的机器未安装 codex，故未实测",
    },
}


def resolve_binary(kind: str, explicit: Optional[str] = None) -> str:
    """定位 agent 可执行文件的**绝对真实路径**。

    关键坑：fnm multicli/nvm 的 shell 专属 shim 路径（如
    `/run/user/1002/fnm_multishells/541_.../bin/claude`）在新开的 tmux pane 里
    **不存在**。必须解析成真实路径，否则 pane 里 command not found。
    """
    if explicit:
        path = os.path.realpath(os.path.expanduser(explicit))
        if not os.path.exists(path):
            raise TmuxError(f"指定的可执行文件不存在：{path}")
        return path
    spec = AGENT_SPECS.get(kind)
    if spec is None:
        raise TmuxError(f"未知 agent 类型 {kind!r}，可选：{', '.join(AGENT_SPECS)}")
    found = shutil.which(spec["bin"])
    if not found:
        raise TmuxError(f"PATH 上找不到 {spec['bin']}；请用 --bin 指定绝对路径")
    return os.path.realpath(found)


def start_agent(
    kind: str,
    cwd: Optional[str] = None,
    session: Optional[str] = None,
    binary: Optional[str] = None,
    extra_args: Optional[Iterable[str]] = None,
    width: int = 200,
    height: int = 50,
    ready_timeout_ms: int = 120_000,
    accept: "re.Pattern[str] | None" = None,
    log: Optional[Callable[[str], None]] = None,
) -> TmuxAgent:
    """启动 agent 并自动清掉所有启动弹窗，直到出现输入提示符。

    弹窗的**选项顺序和文案会随版本变化**（实测：Claude Code 2.1.76 的信任弹窗
    默认在 "Yes"，2.1.270 默认在 "No, exit"）。所以这里是读屏驱动的，
    绝不硬编码"按 Down 再 Enter"。
    """
    log = log or (lambda _m: None)
    # 各 agent 的肯定选项文案不同，且可能带序号：
    #   Claude Code: "Yes, I trust this folder" / "Yes, I accept"
    #   Codex:       "1. Yes, continue"
    # cursor_label() 已剥掉序号，这里只需匹配肯定的开头。
    accept = accept or re.compile(r"^(yes|trust|accept|continue|allow|approve)\b", re.IGNORECASE)
    spec = AGENT_SPECS.get(kind)
    if spec is None:
        raise TmuxError(f"未知 agent 类型 {kind!r}，可选：{', '.join(AGENT_SPECS)}")

    binary = resolve_binary(kind, binary)
    agent = TmuxAgent(
        name=session or f"{kind}-{int(time.time()) % 100000}",
        cwd=cwd,
        width=width,
        height=height,
        log=log,
    )
    agent.open()
    agent.configure_pane()
    log(f"会话 {agent.name} 已就绪，工作目录 {agent.cwd}；接管：{agent.attach_hint()}")

    argv = [binary, *spec["args"], *(extra_args or [])]
    log(f"启动：{' '.join(argv)}")
    # 用 shlex.quote 后再交给 shell：路径含空格时不会断成两个词，
    # 而普通 flag（如 --dangerously-skip-permissions）quote 后原样不变。
    agent.send_text(shlex.join(argv), enter=True)

    deadline = time.monotonic() + ready_timeout_ms / 1000
    while True:
        time.sleep(1.5)
        screen = agent.read()

        if is_menu(screen):
            handled, selected = accept_menu_option(agent, accept, log=log)
            if handled:
                continue
            raise TmuxError(
                "遇到无法自动应答的启动弹窗；光标在 "
                f"{cursor_label(screen)!r}\n{screen}"
            )

        # 没有菜单、且存在光标输入行即视为就绪。
        # 各 agent 的主界面标志不同（Claude Code: "bypass permissions on"；
        # Codex: "permissions: YOLO mode"），所以只依赖通用的光标输入行。
        if cursor_label(screen) is not None or re.search(r"permissions:.*(yolo|bypass)|bypass permissions on", screen, re.IGNORECASE):
            log("agent 已就绪，可以发送任务")
            return agent

        if time.monotonic() > deadline:
            raise TmuxError(f"超时未到达输入提示符\n{screen}")


# ---------------------------------------------------------------------------
# 提交任务
# ---------------------------------------------------------------------------

# 助手输出的行前缀。各 agent 不同：Claude Code 用 ●，Codex 用 ■（错误）等。
_REPLY_PREFIX = re.compile(r"^\s*[●•■⏺◦◆]\s?")
# 终止一个回复块：输入框行（光标符号开头）或分隔线。
_REPLY_STOP = re.compile(r"^\s*[" + re.escape(_CURSOR_GLYPHS) + r"─━═]")


def extract_reply(screen: str) -> str:
    """从渲染后的屏幕里尽力提取助手回复（取最后一个非空块）。

    这是**便利函数，不是可靠解析**：TUI 的渲染会随 agent 与版本变化
    （Claude Code 用 `●`，Codex 用 `■`/`•`，还可能带工具调用行、spinner、
    折行）。屏幕上可能有多个历史轮次，这里取最后一个。
    **权威输出始终是整屏文本**——用 `result.screen` 或 `agent.read()`。
    """
    blocks: list[str] = []
    current: Optional[list[str]] = None
    for line in str(screen).splitlines():
        if _REPLY_PREFIX.match(line):
            if current is not None:
                blocks.append("\n".join(current).strip())
            current = [_REPLY_PREFIX.sub("", line)]
        elif current is not None:
            if _REPLY_STOP.match(line) or not line.strip():
                blocks.append("\n".join(current).strip())
                current = None
            else:
                current.append(line)
    if current is not None:
        blocks.append("\n".join(current).strip())
    return next((b for b in reversed(blocks) if b), "")


def submit_task(
    agent: TmuxAgent,
    task: str,
    wait_for_file: Optional[str] = None,
    timeout_ms: int = 300_000,
    quiet_ms: int = 4000,
    log: Optional[Callable[[str], None]] = None,
) -> TaskResult:
    """提交一个任务并等待完成。

    完成判定优先用**事实**：调用方知道会产生什么副作用时（wait_for_file），
    轮询文件存在性远胜于猜屏幕。没有事实可等时才退回 settle 启发式。
    """
    log = log or (lambda _m: None)
    started = time.monotonic()

    agent.send_text(task, enter=True)
    log(f"已发送任务（{len(task)} 字符）")

    file_appeared = False
    if wait_for_file:
        target = os.path.abspath(os.path.expanduser(wait_for_file))
        while (time.monotonic() - started) * 1000 < timeout_ms:
            if os.path.exists(target):
                file_appeared = True
                log(f"观察到副作用：{target}")
                break
            time.sleep(1.0)

    remaining = max(1000, timeout_ms - int((time.monotonic() - started) * 1000))
    settle = agent.wait_for_settle(quiet_ms=quiet_ms, timeout_ms=remaining, min_wait_ms=800)

    return TaskResult(
        reply=extract_reply(settle.text),
        screen=settle.text,
        settled=settle.ok,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        file_appeared=file_appeared,
        session=agent.name,
        attach=agent.attach_hint(),
    )


def run_agent_task(
    kind: str,
    task: str,
    cwd: Optional[str] = None,
    session: Optional[str] = None,
    binary: Optional[str] = None,
    extra_args: Optional[Iterable[str]] = None,
    wait_for_file: Optional[str] = None,
    timeout_ms: int = 300_000,
    quiet_ms: int = 4000,
    keep_alive: bool = False,
    log: Optional[Callable[[str], None]] = None,
) -> TaskResult:
    """一步到位：启动 → 过弹窗 → 发任务 → 等完成 → 返回。"""
    agent = start_agent(kind, cwd=cwd, session=session, binary=binary, extra_args=extra_args, log=log)
    try:
        return submit_task(
            agent, task,
            wait_for_file=wait_for_file,
            timeout_ms=timeout_ms,
            quiet_ms=quiet_ms,
            log=log,
        )
    finally:
        if not keep_alive:
            agent.close()
