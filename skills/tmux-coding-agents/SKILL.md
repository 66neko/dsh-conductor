---
name: tmux-coding-agents
description: 用 tmux 在独立会话中启动并驱动 Claude Code 或 Codex 的交互式 TUI，完成任务。适用于需要独立进程与会话、指定工作目录、全权限免确认、自动应答启动对话框、多轮追加指令、或人工随时 attach 接管同一个活动会话的场景。包含 Python 脚本：一条命令跑完任务、读屏、发按键、等待完成、列出与关闭会话。
---

# 用 tmux 驱动 Claude Code / Codex

在**独立的 tmux 会话**里启动交互式 agent，用 `send-keys` 当键盘、`capture-pane` 当屏幕，
全程可程序化驱动，会话同时保持人工可接管。

**Claude Code 与 Codex 是对等选项，用哪个由调用方指定**（`--agent claude` / `--agent codex`），
本 skill 不预设默认、也不替调用方选择。

## 快速开始

脚本在本 skill 目录的 `scripts/` 下，只用 Python 标准库，无需安装依赖。

```bash
S=<本skill目录>/scripts

# 一步到位：启动 → 自动过启动弹窗 → 发任务 → 等完成 → 打印回复
python3 $S/agent_task.py run --agent claude --cwd /path/to/proj \
    --task "创建 hello.html" --wait-file /path/to/proj/hello.html

python3 $S/agent_task.py run --agent codex --cwd /path/to/proj \
    --task "创建 hello.html" --wait-file /path/to/proj/hello.html

# 保留会话供人工接管
python3 $S/agent_task.py run --agent claude --cwd /path/to/proj \
    --task "修复失败的测试" --keep

# 分步操作（长任务推荐，方便中途查看与干预）
python3 $S/agent_task.py start  --agent claude --cwd /path/to/proj --session cc1
python3 $S/agent_task.py read   --session cc1 --plain
python3 $S/agent_task.py send   --session cc1 --text "继续"
python3 $S/agent_task.py settle --session cc1
python3 $S/agent_task.py list
python3 $S/agent_task.py close  --session cc1
```

人工接管任何时候都可以：`tmux attach -t cc1`（`Ctrl-b d` 脱离，不影响会话运行）。

## 标准工作流

1. 确认 tmux 可用（`tmux -V`）与目标 agent 的可执行文件路径。
2. 用 `run`（一次性）或 `start`（保留会话分步操作）启动。
   **首次在新目录会遇到信任弹窗，脚本会自动应答。**
3. 需要观察就用 `read --plain`；需要干预就用 `send` / `keys`。
4. 判定完成：**优先给 `--wait-file`**（见下文"事实优先于启发式"）。
5. 用完 `close`，或用 `--keep` / `start` 保留会话供接管。

## 常用操作配方

```bash
S=<本skill目录>/scripts

# 多轮追问：同一个会话里继续
python3 $S/agent_task.py send --session cc1 --text "把测试也补上"

# 中断正在跑的命令（Ctrl-C）
python3 $S/agent_task.py keys --session cc1 C-c

# 应答一个选择弹窗：读屏确认光标位置后按需下移
python3 $S/agent_task.py read  --session cc1 --plain
python3 $S/agent_task.py keys  --session cc1 Down Enter

# 从 stdin 传多行任务
cat task.md | python3 $S/agent_task.py send --session cc1

# 并行多个独立会话（互不干扰，各自独立进程）
python3 $S/agent_task.py start --agent claude --cwd /projA --session job-a
python3 $S/agent_task.py start --agent codex  --cwd /projB --session job-b
```

## 三个必须理解的设计点

### 1. 完成判定：事实优先于启发式

`settle` 判断"屏幕不再有意义地变化"，是**启发式**。工作中的 TUI 一直在动
（spinner 旋转帧、已用时间、token 计数、进度条），所以实现里先把易变部分脱敏
（数字→`#`，spinner→`~`，剥 ANSI）再比较结构变化。

**这个启发式是故意有损的**：如果任务唯一的进度表现就是变化的数字，会被误判为已完成。

所以**调用方知道会产生什么副作用时，一定要给 `--wait-file`**——轮询文件存在性是
**事实**。CLI 覆盖不到的外部条件，在 Python 里用 `agent.wait_for(正则)` 锚定真实文本
（输入提示符、完成横幅、错误信息）。

### 2. 启动弹窗必须读屏驱动，不能硬编码按键

弹窗的**选项顺序、文案、甚至光标符号都因 agent 和版本而异**，实测：

| agent / 版本 | 弹窗 | 光标符号 | 确认提示语 | 默认光标位置 |
|---|---|---|---|---|
| Claude Code 2.1.76 | trust：`1. Yes, I trust this folder` / `2. No, exit` | `❯` | `Enter to confirm · Esc to cancel` | **Yes 在前** |
| Claude Code 2.1.270 | trust：`No, exit` / `Yes, I trust this folder` | `❯` | 同上 | **No 在前** |
| Claude Code 2.1.270 | bypass：`No, exit` / `Yes, I accept` | `❯` | 同上 | **No 在前** |
| Codex 0.154.0 | trust：`1. Yes, continue` / `2. No, quit` | **`›`** | **`Press enter to continue`** | Yes 在前 |

所以硬编码"按 Down 再 Enter"或"找 `❯`"都会失败。本 skill 的做法是**三个都做成可配置的多值**：

- **光标符号**：`❯›»▸▶→`（`_CURSOR_GLYPHS`）。刻意**不含**裸露的 `>`——Codex 欢迎语里有
  `> You are in /tmp`、标题框里有 `>_`，会把普通文字误判成光标。
- **确认提示语**：`Enter to confirm` / `Esc to cancel` / `Press enter to continue` 等（`_MENU_HINT`）。
- **肯定选项**：匹配 `^(yes|trust|accept|continue|allow|approve)`，并且先剥掉 `1.` 这类序号前缀
  （Codex 的选项带编号，否则 `^Yes` 匹配不上）。

然后逐格下移光标并重新读屏，直到高亮项匹配——与顺序和数量无关。

### 3. 光标符号和输入框是同一个符号

**菜单光标和聊天输入框用的是同一个符号**（Claude Code 都是 `❯`，Codex 都是 `›`），
单看符号分不清"这是一个需要应答的菜单"还是"这是聊天输入框"。

可靠判别是**确认提示语**：菜单有，输入框永远没有。代码里就是 `is_menu()`。

同理，就绪判定用的是"存在光标输入行且不是菜单"，所以对两个 agent 都成立。

## 关于 `extract_reply` 的可靠性

`extract_reply()` 是**便利函数，不是可靠解析**。各 agent 的助手输出前缀不同
（Claude Code 用 `●`，Codex 用 `■`/`•`），渲染还可能带工具调用行、spinner、折行。
它取屏幕上最后一个非空块，够用但不保证准确。

**权威输出始终是整屏文本**：`result.screen`（Python）/ `run --show-screen`（CLI）/
`read --plain`。需要精确提取时，用 `wait_for(正则)` 锚定真实文本，或直接取整屏自己解析。

## 必须知道的坑

| 坑 | 后果 | 本 skill 的处理 |
|---|---|---|
| `send-keys` 不加 `-l` | `Enter`、`C-c`、`Up` 等词被当**按键名**；任务文本里出现 "Enter" 就会变成回车 | 一律 `send-keys -l --` |
| TUI 收到输入要重绘，紧跟的回车可能被吞 | 任务看似发出但没提交，文字留在输入框里 | `send_text` 在文本与回车之间默认停顿 120ms（`enter_delay_ms`） |
| detached 会话默认 80×24 | 全屏 TUI 按这个尺寸重排、界面挤烂、就绪判断失准 | `-x 200 -y 50` 显式指定 |
| `capture-pane` 不加 `-J` | 终端软换行被当多行，解析全乱 | 默认加 `-J` |
| pane 底部是一堆空行 | 直接 `tail` 取到空白，误以为程序没输出 | `read --plain` 去空行 |
| fnm/nvm 的 shell 专属 shim 路径 | `/run/user/.../fnm_multishells/.../bin/claude` 在新 pane 里**不存在** | `resolve_binary()` 用 `realpath` 解析真实路径 |
| tmux 新 pane 继承**服务端**的 PATH | 服务端可能是很久前启动的，pane 里找不到 `node` | `new-session -e PATH=<当前PATH>` 显式注入 |
| tmux 会话名含 `.` `:` | 创建失败 | `safe_session_name()` 清洗 |
| 拼 shell 字符串调 tmux | 引号地狱、注入风险 | 全程 `subprocess.run([...argv])` |

## Python API

```python
import sys; sys.path.insert(0, "<本skill目录>/scripts")
from tmux_agent import start_agent, submit_task, TmuxAgent

# 启动并自动过弹窗（kind 由调用方决定：claude 或 codex）
agent = start_agent("claude", cwd="/path/to/proj", session="cc1")

# 用事实判定完成
result = submit_task(agent, "创建 hello.html",
                     wait_for_file="/path/to/proj/hello.html",
                     timeout_ms=300_000)
print(result.reply)          # 提取出的助手回复
print(result.settled)        # 是否稳定
print(result.file_appeared)  # 副作用是否出现
print(result.attach)         # tmux attach -t cc1

# 接管一个**已经存在**的会话（人工或其他程序建的）
agent = TmuxAgent.attach("cc1")   # 不要用构造函数去接既有会话

# 低层控制
screen = agent.read()                    # 当前屏幕全文
agent.send_text("继续", enter=True)       # 字面量输入
agent.send_keys("Enter")                 # 命名按键
agent.send_keys("C-c")                   # 中断
agent.wait_for(r"❯\s*$", timeout_ms=60_000)   # 等界面回到输入提示符
agent.wait_for_settle(quiet_ms=4000)     # 启发式等待
agent.close()
```

主要对象：

| 成员 | 作用 |
|---|---|
| `TmuxAgent(name, cwd, width, height, settle_ms)` | 一个被驱动的会话（**新建**用 `start_agent`，接管已有用 `.attach()`） |
| `TmuxAgent.attach(name)` | 接管已存在的会话（人工或其他程序建的） |
| `.open()` / `.close()` / `.configure_pane()` | 生命周期 |
| `.read(scrollback=None)` / `.screen_lines()` | 抓屏 |
| `.send_text(text, enter=False)` | 字面量输入（内部用 `-l`） |
| `.send_keys(*keys)` | 命名按键 |
| `.wait_for_settle(quiet_ms, timeout_ms)` | 启发式等稳定 |
| `.wait_for(pattern, timeout_ms)` | 等正则匹配（**优先用这个**） |
| `.ask(text)` | 输入 + 回车 + 等稳定 |
| `.attach_hint()` | 人工接管命令 |
| `TmuxAgent.list_sessions()` / `.has_session(name)` | 会话查询 |
| `start_agent(kind, ...)` | 启动并自动过弹窗 |
| `submit_task(agent, task, wait_for_file=..., ...)` | 发任务并等完成 |
| `extract_reply(screen)` | 从屏幕提取助手回复 |

## 支持哪些 agent

`AGENT_SPECS`（在 `scripts/tmux_agent.py`）定义每个 agent 的可执行文件名与启动参数。
两个 agent 地位对等，用哪个由调用方通过 `--agent` 指定：

| agent | 启动参数 | 验证状态 |
|---|---|---|
| `claude` | `--dangerously-skip-permissions` | **端到端已实测**（Claude Code 2.1.270）：启动、过弹窗、完成任务、产出文件 |
| `codex` | `--dangerously-bypass-approvals-and-sandbox` | **启动路径已实测**（Codex 0.154.0）：自动过信任弹窗、到达输入提示符、`permissions: YOLO mode` |

> Codex 在本机的**模型调用**返回 `401 Unauthorized`（该机器的 codex 走第三方镜像端点且凭据缺失），
> 属环境配置问题，与本 skill 无关。启动与界面驱动部分已完整验证。

两者的启动参数都是**全权限、免确认**。若某个 agent 的参数在其版本上不同，
用 `--args` 覆盖启动参数（**必须放在命令最后**，`REMAINDER` 会吞掉其后的所有内容）：

```bash
python3 $S/agent_task.py start --agent codex --cwd /proj --session cx1 \
    --verbose --args --some-other-flag
```

确认可用后把正确的参数写进 `AGENT_SPECS`，供后续复用。

**新增其他 agent**：在 `AGENT_SPECS` 里加一项 `{bin, args}` 即可，
CLI 的 `--agent` 选项会自动出现，无需改其他代码。用 `start --verbose` 加
`read --plain` 观察实际界面来调试。若新 agent 的光标符号或确认提示语不同，
在 `_CURSOR_GLYPHS` / `_MENU_HINT` 里补上对应值。

## 环境要求

- `tmux`（脚本会检查，缺失时报错提示 `apt install tmux`）
- Python 3.8+（只用标准库）
- 目标 agent 的可执行文件在 PATH 上，或用 `--bin` 指定绝对路径
- 本 skill 开发环境：tmux 3.4 / Python 3.12 / Claude Code 2.1.270（WSL2）

## 启动参数说明

本 skill 默认以**全权限、免确认**方式启动 agent：会话内的 agent 可以执行调用方
本人能执行的任何命令。这是 `AGENT_SPECS` 里 `args` 的内容，可按需修改或覆盖。
