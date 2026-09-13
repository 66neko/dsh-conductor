# 架构

## 三层职责

```
┌──────────────┐  JSON-RPC/stdio  ┌────────────────────┐   tmux + skill   ┌──────────────┐
│ 调用方       │ ───────────────► │ DSH 运行时         │ ───────────────► │ 下级 agent   │
│ (Python)     │                  │ 编排 + 独立验收    │                  │ Claude/Codex │
│              │ ◄─────────────── │                    │ ◄─────────────── │              │
└──────────────┘  result.json     └────────────────────┘  不通过则追加指令  └──────────────┘
```

| 层 | 做什么 | **不**做什么 |
|---|---|---|
| `conductor/cli.py` | 参数解析、建状态目录、驱动 DSH、显示进度、读回结论 | 不解释模型输出，不判断工作质量 |
| DSH（外部进程） | 委派、**独立验收**、不通过则返工、落盘结构化结论 | 不直接改产物 |
| `skills/tmux-coding-agents` | 启动/驱动交互式 TUI：读屏、发按键、等待完成 | 不判断任务是否完成 |
| 下级 agent | 实际干活 | —— |

**为什么验收必须由 DSH 做而不是 conductor 做**：验收需要读文件、跑命令、做判断，
这些正是 agent 的能力。conductor 若自己实现一套，等于重写一个 agent 且更弱。

## 完成判定：三处事实，没有一处靠猜

整个流程里每个"结束了"都由可核实的事实决定：

| 判定 | 依据 | 类型 |
|---|---|---|
| DSH 这一轮结束了吗 | `turn/end` 事件的 `reason.kind` + `session.status == idle` | **协议事实** |
| 下级 agent 干完了吗 | skill 的 `--wait-file`（文件存在）优先于 `settle` | **文件事实** / 启发式兜底 |
| DSH 验收通过了吗 | `.dsh-orchestrator/result.json` 存在且 `status == "accepted"` | **文件事实** |

`conductor` **不解析 DSH 的自然语言回复**来判断成败——那只打印给人看。
运行前会删掉旧的 `result.json`，所以它的存在必然代表本次运行产生了它。

### 为什么要有这条原则

两个真实的坑：

1. **DSH 会把长耗时的委派当后台任务跑。** 它用 `bash` 启动 `agent_task.py run` 后立刻
   拿到 `started background job bash-1`，再轮询 `job_output`。如果调用方靠"stdout 有没有
   新内容"判断进度，这段时间看起来就是卡死。
2. **下级 agent 的自述不是证据。** 它说"已完成"，可能是真完成、可能只写了一半、
   可能写错了地方。所以编排指令里写死：**必须自己 `ls`/`read`/实际跑校验。**

## 日志来源：为什么不抓屏

一开始的实现用 `tmux capture-pane` 轮询下级屏幕，只能看到**可见的那几十行**。
挖下去发现三层递进的原因：

### 1. 全屏 TUI 运行在备用屏幕上

```
$ tmux display-message -p -t <session> '#{history_size} 行历史'
2 行历史 / 30 行可见
```

备用屏幕**没有滚动缓冲**，所以 `capture-pane -S -N` 永远取不到滚出去的内容。
试过 `set-option alternate-screen off`，也只从 0 行变成 2 行——不解决问题。

### 2. 更本质：TUI 发送的是「屏幕差分」，不是文本流

全屏 TUI 重绘而不是换行滚动，所以 tmux 根本没有内容可以存进滚动历史。
**这不是配置问题，是机制问题。**

### 3. `tmux pipe-pane` 也不可用

它是给终端回放用的原始字节流。剥掉 ANSI 之后：

```
Accessingworkspace:              ← 空格没了（TUI 用光标定位，文字黏在一起）
✻✽✻✶*✢·✢*✶✻✽✻✶*✢·✢*✶           ← spinner 垃圾
（同样的段落重复出现 3 次）        ← TUI 重绘/重排
prfix o / setw sychronize-panes  ← 文字被截断缺字
```

### 正确解法：跟随 agent 自己写的转录

两个 agent 都会把完整会话写到磁盘：

| agent | 转录路径 |
|---|---|
| Claude Code | `~/.claude/projects/<工作目录把 / 换成 ->/<session-id>.jsonl` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<uuid>.jsonl` |

实测对比（同一任务）：

| 来源 | 拿到的内容 |
|---|---|
| `capture-pane` 抓屏 | 30 行视口里的换行碎片，段落被硬切 |
| **转录跟随** | **完整 4 段正文、格式与空行全对、无截断** |

转录是结构化 JSONL，还能拿到屏幕上看不到的东西：`thinking` 块、完整 `tool_use`
参数、token 用量。

### 陈旧转录的陷阱

跟随转录最容易犯的错是**误抓上一次任务留下的旧文件**——那样日志会显示过期内容，
**比什么都不显示更坏**。所以 `transcript.py` 的判据是"相对启动时刻的**变化**"而不是
"文件新不新"：

- 启动时先 `snapshot_existing()` 拍下已存在文件的路径与大小
- 精确目录（由 cwd 推算）里：只认**启动时不存在的新文件**，或**已知文件变大了**
- 回退目录（全局搜索）里：**只认启动时完全不存在的新文件**

## 指令模板的三个要点

`conductor/prompt.py` 里的 `PROMPT_TEMPLATE` 决定 DSH 怎么干活。三条经验都是踩出来的：

1. **必须明说"下级 agent 的自述不算证据"**，否则 DSH 会把下级的"已完成"直接转述成结论。
2. **任务文本要走文件**（`--task "$(cat task.txt)"`），不能拼进命令行——
   任意自然语言里的引号、换行、`$` 都会破坏 shell 命令。
3. **必须指定结构化落盘**，并明确 `accepted` 只在**独立验证通过**时才可用。

## 环境前提

`DSH_PERMISSION_MODE` 固定为 `danger-full-access`。原因：DSH 要靠它的 `bash` 工具
执行 `agent_task.py` 和验证命令。而 confining 沙箱在 Linux 需要 `bwrap` 或 `landlock`
才可用——两者都缺时，**任何 confining 模式下 bash 都会被整体拒绝**
（`SANDBOX_UNAVAILABLE`，fail-closed），整个工作流卡在第一步。
