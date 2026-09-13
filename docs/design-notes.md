# 设计取舍与踩坑记录

记录做这个项目时的关键决策和踩过的坑，供后续修改时参考。

## 取舍

### 为什么用 `dsh --profile sdk` 而不是 `--profile headless`

`headless` 是"一条命令跑一个任务、打印最终答案、退出"，够简单。但它拿不到结构化状态：
**分不清"模型跑完了但报错"和"正常完成"**——只有 exit code 0/1。

`sdk` profile 在 stdio 上说 JSON-RPC，能拿到 `turn/end.reason.kind` 与全部会话事件。
编排需要观察 DSH 在做什么，所以选它。

两者的代价：`sdk` 的 stdout 是纯协议帧，诊断全在 stderr，不能有杂音。

### 为什么自己实现协议客户端而不用官方 Python SDK

官方 `deepseek-harness-sdk` 需要安装 wheel + 匹配的原生运行时。
自实现只有一个目的：**零依赖**，在任何有 Python 的机器上直接可跑。
协议本身很简单（3 个请求方法 + 4 个通知），成本可控。

若你的环境已装官方 SDK，替换 `conductor/dsh.py` 即可，其余不动。

### 为什么 skill 用软链而不是复制

skill 是"下级 agent 怎么被驱动"的**唯一知识源**。复制成两份必然漂移——
改了一处忘了另一处，模型读到的说明与实际代码不符。

`conductor install-skill` 建软链，并在目标已是**真实目录**时先备份再替换
（绝不直接删，用户可能改过）。

### 为什么每轮只做一次委派循环而不是无限重试

`--max-attempts` 默认 2。理由：每次重试都是一轮完整的下级 agent 执行（几十秒 + 真金白银）。
无限重试在验收标准写错时会把成本烧光。宁可失败返回 `rejected` 让调用方决定。

## 踩过的坑

### 1. `send-keys` 不加 `-l`，文本里的 "Enter" 变成回车

tmux 的 `send-keys` 默认把 `Enter`、`C-c`、`Up` 这类词当**按键名**。
如果任务文本里出现 "Enter" 这个词，就会被解释成回车而不是文字。

**修法**：一律用 `send-keys -l --`（`-l` 强制字面量）。已实测：
`echo "A B $HOME $(date +%Y) Enter"` 能原样正确执行。

### 2. TUI 收到输入要重绘，紧跟的回车被吞

现象：任务文字留在输入框里没提交（实测 Codex 偶发）。

**修法**：`send_text` 在文本与回车之间默认停顿 120ms（`enter_delay_ms`）。

### 3. detached tmux 会话默认 80×24

全屏 TUI 按这个尺寸重排，界面挤烂、就绪判断失准。

**修法**：`-x 200 -y 50` 显式指定几何尺寸。

### 4. fnm/nvm 的 shell 专属 shim 路径在新 pane 里不存在

`which claude` 在 fnm 下会给出 `/run/user/.../fnm_multishells/.../bin/claude`，
这个路径只在当前 shell 有效，新的 tmux pane 里**不存在**。

**修法**：`resolve_binary()` 用 `os.path.realpath()` 解析真实路径。

### 5. tmux 新 pane 继承的是**服务端**的 PATH

tmux 服务端可能是很久以前用另一份 PATH 启动的，导致 pane 里找不到 `node`。

**修法**：`new-session -e PATH=<当前PATH>` 显式注入（tmux ≥ 3.0）。

### 6. `❯` 是歧义的

Claude Code 的**菜单光标和聊天输入框都用 `❯`**，单看符号分不清。

**修法**：用确认提示语判别——菜单有 `Enter to confirm · Esc to cancel`，输入框永远没有。

### 7. 弹窗的选项顺序和光标符号会随版本/agent 变

实测：

| agent / 版本 | 光标符号 | 确认提示语 | 默认光标位置 |
|---|---|---|---|
| Claude Code 2.1.76 | `❯` | `Enter to confirm · Esc to cancel` | Yes 在前 |
| Claude Code 2.1.270 | `❯` | 同上 | **No 在前** |
| Codex 0.154.0 | **`›`** | **`Press enter to continue`** | Yes 在前 |

硬编码"按 Down 再 Enter"或"找 `❯`"都会失败——前者会在升级后**静默点下 "No, exit"**。

**修法**：光标符号、确认提示语、肯定选项都做成**多值**，并逐格移动光标重新读屏直到匹配。

### 8. 抓屏拿不到完整日志

见 [`architecture.md`](architecture.md#日志来源为什么不抓屏)。三层原因：
备用屏幕无历史 → TUI 发屏幕差分 → `pipe-pane` 不可用。

### 9. 转录跟随会误抓陈旧文件

第一版抓到了上一次会话的转录，日志显示过期内容——**比什么都不显示更坏**。

**修法**：判据改成"相对启动时刻的变化"（启动快照 + 新文件或文件变大）。

### 10. unref 的轮询定时器让进程提前退出

早先的 Node 版本里：轮询定时器 `unref()` 后，子进程一退出事件循环就空了，
进程在**队列还有任务时**就退出，静默丢任务。

**修法**：主循环的 sleep 必须是 referenced 的。

### 11. DSH 的默认 bash 超时是 60 秒

base bundle 里 `bash-sandbox.timeoutMs: 60000`，单次调用上限 600 秒，
且**不能设为无限**（`assertPositiveFinite` 拒绝 0/Infinity）。

如果下级任务耗时较长，注意 DSH 侧 `agent_task.py run` 的调用会被这个超时影响——
编排指令里给的 `--timeout-ms 900000` 是给 skill 的，DSH 自己的 bash 超时另算。
必要时通过 `$DSH_HOME/settings.yaml` 的 `shell:` 段调大。

## 恢复手段：转录是安全网

转录与文件历史不只是进度显示，出意外时能救命：

| 位置 | 内容 |
|---|---|
| `~/.claude/projects/<slug>/<session>.jsonl` | 全部对话、`tool_use`（含**完整参数**，即写文件时的全文） |
| `~/.claude/file-history/<session>/<hash>@vN` | 每次编辑前的**文件内容快照** |
| `$DSH_HOME/sessions/<slug>/<session>/session.v3.jsonl.zstd` | DSH 侧全部持久化事件 |

恢复办法：从 `file-history` 取最接近最终状态的快照，再从转录里取出该快照时间点
**之后**的编辑操作按序重放。

注意 DSH 的 `.zstd` 是多个独立 zstd 帧串接，Python 标准库没有 zstd，
可用 Node 的 `zlib.zstdDecompressSync` 逐帧解压。

> 这套办法实测有效：一次误删后完整还原了 30894 字节的产物，
> 且还原后的字节数与 DSH 记录中的字节数**完全一致**。
