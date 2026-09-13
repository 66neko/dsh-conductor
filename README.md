# dsh-conductor

**让 [DSH（DeepSeek Harness）](https://github.com/deepseek-ai/deepseek-harness) 编排下级编码 agent 干活，并且独立验收后才把结论交回你的脚本。**

你只负责下达任务和验收标准；剩下的委派、监督、核实、返工由 DSH 完成。

```bash
conductor run \
  --workspace ./my-project \
  --task "创建 hello.html，一个最小但合法的 HTML5 页面，h1 文本是 Hello World" \
  --verify "hello.html 存在，以 <!DOCTYPE html> 开头，且恰好含一个 <h1>Hello World</h1>"

# → 验收结论 : ✅ ACCEPTED
# → 产物文件 : ['hello.html']
# → 验收依据 : 第1轮 ls 确认存在（225 字节）；read 通读全文；python3 html.parser
#              解析得 h1=='Hello World' 且唯一，doctype 位于首行 → 通过
```

## 项目介绍

一句话：**这是一个「带独立验收的多 agent 编排器」——编排交给 DSH，干活交给 Claude Code / Codex，验收由 DSH 独立完成。**

它解决的是单层 agent 的结构性问题：**下级 agent 说"我完成了"，不等于它真的完成了。**
如果编排层只是把下级的自述转述给你，验收就是橡皮图章。
所以本项目的编排指令里写死了一条：**下级 agent 的自述不算证据，必须自己动手核实。**

实测这条约束是真的生效的——给一个与产物故意冲突的验收标准，DSH 会判不通过、
向同一个会话追加修正要求、重验通过后才落盘结论（见「验收是真的」一节）。

### 三层结构

```
┌──────────────┐  JSON-RPC/stdio  ┌────────────────────┐   tmux + skill   ┌──────────────┐
│ 你的脚本     │ ───────────────► │ DSH 运行时         │ ───────────────► │ Claude Code  │
│ (Python)     │                  │ 编排 + 独立验收    │                  │ 或 Codex     │
│              │ ◄─────────────── │                    │ ◄─────────────── │              │
└──────────────┘  result.json     └────────────────────┘  不通过则追加指令  └──────────────┘
                                          │
                                  自己动手核实：ls / read / 实际跑校验
```

| 层 | 职责 |
|---|---|
| `conductor`（本项目） | 下达任务、驱动 DSH、实时显示进度、读回结论 |
| **DSH** | 委派、**独立验收**、不通过则返工、落盘结构化结论 |
| `tmux-coding-agents` skill | 在独立 tmux 会话里启动并驱动交互式 TUI（读屏、发按键、等待完成） |
| 下级 agent | 实际干活：Claude Code 或 Codex |

## 依赖

| 依赖 | 必需 | 说明 |
|---|---|---|
| Python | ✅ 3.8+ | 本项目**零第三方依赖**，全部标准库 |
| `tmux` | ✅ | skill 用它隔离会话并驱动交互式 TUI（`apt install tmux`） |
| **DSH — DeepSeek Harness** | ✅ | 编排与验收的执行者 |
| Claude Code / Codex | ✅ 至少一个 | 下级 agent，用哪个由 `--agent` 指定 |
| DeepSeek 凭据 | ✅ | DSH 调用模型所需（`$DSH_HOME/.credentials.yaml` 或 `DEEPSEEK_API_KEY`） |

### 关于 DSH

> **DSH = DeepSeek Harness**（命令名 `dsh`），由 [DeepSeek AI](https://deepseek.com) 开源的 agent harness，
> 采用 everything-is-a-plugin 架构。
>
> - 仓库：<https://github.com/deepseek-ai/deepseek-harness>
> - 文档：<https://deepseek-harness.github.io/deepseek-harness/>
>
> 本项目通过 `dsh --profile sdk` 的 **JSON-RPC over stdio** 协议驱动 DSH，
> 不依赖任何 DSH 内部模块。

## 安装

```bash
git clone <本项目地址>
cd dsh-conductor

# 1) 环境自检（先跑这个，它会把缺什么、怎么修都列出来）
python3 -m conductor doctor

# 2) 安装 skill：把仓库里的 skill 软链到 ~/.dsh/skills/
python3 -m conductor install-skill

# 3) 可选：安装成命令
pip install -e .        # 之后可直接用 `conductor` 而不是 `python3 -m conductor`
```

`doctor` 的输出形如：

```
dsh-conductor 0.1.0 环境自检

  ✅ tmux                      /usr/bin/tmux
  ✅ dsh (DeepSeek Harness)    /path/to/deepseek-harness/apps/cli/lib/bin.js
  ✅ skill tmux-coding-agents  /home/you/.dsh/skills/tmux-coding-agents
  ✅ DSH 凭据                   /home/you/.dsh/.credentials.yaml
  ✅ 下级 agent: claude         /path/to/claude
  ⚠️  下级 agent: codex         未安装（可选，用哪个由 --agent 决定）
```

### 关于 DSH 入口

`conductor` 按此顺序自动定位 `dsh`，找不到才报错：
`--dsh-bin` 参数 → `$DSH_BIN` 环境变量 → PATH 上的 `dsh` → 源码 checkout 的常见路径。

## 使用

```bash
conductor run --workspace <目录> --task "<任务>" [--verify "<验收标准>"] [选项]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--workspace` | 必填 | 下级 agent 的工作目录 |
| `--task` | 必填 | 任务（自然语言） |
| `--verify` | 通用标准 | **DSH 的验收标准，写得越具体验收越可靠** |
| `--agent` | `claude` | 下级 agent：`claude` 或 `codex`（两者地位对等） |
| `--max-attempts` | `2` | 最多委派轮次 |
| `--timeout-ms` | `1800000` | DSH 整体超时 |
| `--model` | `deepseek-flash` | DSH 使用的模型 |
| `--dsh-bin` / `--skill-dir` | 自动 | 覆盖自动解析 |
| `--quiet` | 关 | 关闭实时进度 |
| `--verbose-progress` | 关 | 进度里显示 DSH 的 reasoning |
| `--heartbeat-s` | `10` | 无事件时的心跳间隔 |
| `--transcript-interval-s` | `1` | 下级 agent 转录的轮询间隔 |
| `--transcript-thinking` | 关 | 转录里打印 thinking 块 |
| `--follow-screen` | 关 | 额外直播 tmux 屏幕（当前画面，内容不全） |

**退出码**：`0` = DSH 验收通过；`1` = 未通过或出错；`2` = 用法错误。

### 验收标准怎么写

`--verify` 直接决定验收质量——它写得越具体，DSH 的核实就越实在。

```bash
# 弱：只检查存在性
--verify "文件存在"

# 强：给出可程序化核对的具体断言
--verify "config.json 是合法 JSON；含 version 字段且值为 1.2.0；用 python3 -m json.tool 能通过"
```

## 实时进度

进度全部走 **stderr**（stdout 保持机器可读），分三层：

```
[conductor] 已把任务交给 DSH...
------------------------------------------------------------
[    2.9s] dsh ▶ 第 1 轮开始
[    2.9s] dsh → skill tmux-coding-agents
[    6.6s] dsh → bash Run lower-level Claude agent to create guide.md
[    6.6s] dsh ← ok · 29 字符 started background job bash-1
  ┌ 跟随转录 /home/you/.claude/projects/-tmp-final-tr/97e46e6e-....jsonl
[   18.0s] dsh ··· 等待 job_output 返回（已 11s）               ← 心跳
  │ claude 🔧 Bash cat > /tmp/final-tr/guide.md <<'EOF'
  │ claude 💬 已创建 `/tmp/final-tr/guide.md`，共 101 行、6 个小节：
  │ claude      - **会话** — 新建、列出、attach/detach、重命名、切换
[   51.1s] dsh ← ok · 167 字符 --- settled=True 用时=42716ms ...
[   52.9s] dsh 💬 Agent self-reports success. Now step 2: independent verification...
[   62.6s] dsh ■ 第 1 轮结束：completed
```

| 输出 | 来源 | 作用 |
|---|---|---|
| `▶ 💬 → ← ■` | DSH 的协议事件 | DSH 在想什么、调用什么工具、拿到什么结果 |
| `···` 心跳 | 无事件时定时 | 消除"卡住不动"的错觉，并显示正在等哪个工具 |
| `│ claude` | **下级 agent 自己的 JSONL 转录** | 下级 agent 的**完整**输出 |

> **为什么内容来自转录而不是抓屏**：全屏 TUI 运行在备用屏幕上，实测 tmux 的
> `#{history_size}` 恒为 0，抓屏永远只能拿到可见的那几十行；更本质的是 TUI 发送的是
> **屏幕差分**而非文本流，tmux 根本没有内容可存进滚动历史。
> 完整日志只能从 agent 自己写的转录里拿。细节见 [`docs/architecture.md`](docs/architecture.md)。

> **提示**：若你写 `2>&1 | tail`，`tail` 会缓冲整条管道，看起来仍像卡住。
> 直接看终端，或 `> log.txt 2>&1` 后 `tail -f log.txt`。

## 结果

运行中的中间产物集中放在工作目录下的 `.dsh-orchestrator/`：

| 文件 | 内容 |
|---|---|
| `task.txt` | 任务原文（供 DSH 用 `cat` 读取，避免命令行转义问题） |
| `result.json` | **DSH 的结构化验收结论** |

`result.json` 结构：

```json
{
  "status": "accepted",
  "task": "用户任务原文",
  "attempts": 2,
  "artifacts": ["产物相对路径"],
  "verification": "DSH 实际执行了哪些验证、观察到什么",
  "claude_last_message": "下级 agent 的最后回复",
  "notes": "遗留问题"
}
```

**调用方靠这个文件判断成败**，而不是解析 DSH 的自然语言回复——
运行前会删掉旧文件，所以它的存在必然代表本次运行产生了它。

建议把 `.dsh-orchestrator/` 加进你项目的 `.gitignore`。

## 验收是真的：实测会拒绝并重试

验收若只是橡皮图章就毫无价值，所以做过对抗性测试。

**任务**：`创建 page.html，h1 文本是 Version A`
**验收标准**：`h1 必须精确等于 'Version B'`（故意与任务冲突）

```
委派轮次 : 2
验收依据 : 第1轮：ls 确认 page.html 存在（209字节）；read 读取并用 python3 html.parser
           解析，得 h1=='Version A'，与 'Version B' 不符 → 判定不通过。
           第2轮：向同一会话追加修正要求并 settle，重新核验 h1=='Version B'；
           另用 grep 确认 <h1> 唯一、全文无残留 'Version A' → 验收通过。
```

DSH 不仅发现了不匹配，还**指出任务文本与验收标准本身冲突**并说明按验收标准执行。

## 目录结构

```
dsh-conductor/
├── AGENTS.md                     # 给 AI agent 的项目约定
├── README.md                     # 本文件
├── pyproject.toml                # 元数据、零依赖声明、conductor 入口
├── conductor/                    # Python 包
│   ├── __init__.py               # 版本与公开 API
│   ├── __main__.py               # python -m conductor
│   ├── cli.py                    # 命令行与编排主流程
│   ├── dsh.py                    # 与 DSH 的 JSON-RPC/stdio 通信
│   ├── progress.py               # DSH 实时进度 + 心跳
│   ├── transcript.py             # 下级 agent 转录跟随（完整日志）
│   └── prompt.py                 # 编排/验收指令模板（改流程只需改这里）
├── skills/
│   └── tmux-coding-agents/       # tmux 驱动能力的唯一源（软链到 ~/.dsh/skills/）
├── docs/
│   ├── architecture.md           # 三层架构、完成判定、日志来源
│   └── design-notes.md           # 关键取舍与踩坑记录
└── examples/
    └── quickstart.py             # 最小调用示例
```

## 与官方 SDK 的关系

`conductor/dsh.py` 是对 `dsh --profile sdk` 的 stdio 协议实现，**零依赖**。
官方的 `deepseek-harness-sdk`（PyPI）提供同样的协议；若你已安装它，
可以只替换 `dsh.py` 这一层，其余保持不变（`pip install -e '.[sdk]'`）。

## 已知边界

1. **验收质量取决于 `--verify`。** 本项目保证的是"DSH 确实做了独立验证"（实测会拒绝、会重试），
   但标准的严格程度由你决定。
2. **两层模型调用，成本与延迟叠加。** 小任务约 40–60 秒，任务越重越划算。
3. **`DSH_PERMISSION_MODE` 固定为 `danger-full-access`。** DSH 要靠 `bash` 工具执行
   skill 与验证命令；在缺少 `bwrap`/`landlock` 的机器上，confining 沙箱会**整体拒绝**
   bash（fail-closed），工作流会卡在第一步。这意味着 DSH 侧不设文件系统限制。
4. 会在工作目录创建 `.dsh-orchestrator/`。
5. 全屏 TUI 的**当前画面**可用 `--follow-screen` 查看，但那不是完整日志。

## 许可

MIT
