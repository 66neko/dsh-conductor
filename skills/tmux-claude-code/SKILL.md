---
name: tmux-claude-code
description: 在 tmux 中监督 Claude Code，提交任务、观察活动与回执、恢复连接故障、代做交互选择并保存失败证据。用于 DSH 委派给 Claude Code 的编码任务。
---

# 在 tmux 中监督 Claude Code

通过 Python 3.13 运行 `scripts/claude_session.py`，使用 `--dangerously-skip-permissions` 启动 worker。
只控制 request/plan 指定的本 agent 会话和工作区，所有返工沿用该会话。不得改用另一 agent。
会话名精确匹配，读屏与输入绑定启动时的 worker pane；切换观察窗口不会改变控制目标。
本轮使用 SDK 创建的私有 tmux socket，run/send/watch/recover/choose/stop 根据 --request
读取同目录 runtime.json；不得自行连接默认 socket 或启动另一服务器。status/capture/close
使用注入的 DSH_CONDUCTOR_SOCKET；手动执行时须传 --socket /tmp/dshc-.../tmux.sock。
调用方观察会话应使用 SDK 返回的 attach_command。

## 委派与文件交接

管理者提供 request、任务文件、receipt 路径和 token，优先原样使用管理者生成的精确命令。
任务正文留在 task 文件中，只将读取文件和交接协议的短指令通过 tmux buffer/paste 传入 TUI。

```bash
python3.13 <skill>/scripts/claude_session.py run \
  --request /absolute/run/request.json \
  --session dsh-claude-unique \
  --task-file /absolute/attempt/task.md \
  --receipt /absolute/attempt/receipt.json \
  --token UNIQUE_TOKEN
```

`run` 立即登记任务并返回 `starting`；随后必须 `watch`，由 watch 在输入界面就绪后提交任务。
不要把 run 的返回当成完成，不要因 watch 返回 running 而重新 run。只有收到有效回执且独立
验收后需要业务返工，才使用 `send`，参数与 run 相同，但取 request 中下一轮的文件和新 token。

每轮 worker 必须将完整回答、关键结论、修改记录、检查结果、阻塞信息写入 receipt 同目录的
非空 UTF-8 `result.md`。重要内容及时落盘，长检查输出另存文件并在报告中引用。最终报告先写
临时文件再原子替换，然后运行控制器提供的 `complete` 命令。`blocked` 也需保存报告。
终端只显示简短进度和路径。收到回执后，DSH 必须读完整报告及相关引用文件，再独立验收工作区。

当 request 的 `include_report=true` 时，控制器还会提供完整报告交接契约。worker 必须在本轮
目录保存 `subtask-reports.json`（schema_version=1、绑定本轮 receipt_token、subtasks 数组）。
没有内部子任务也要保存空数组；如有委派，开始前登记并持续更新所有层级子任务，父任务先登记。
每项包含唯一 id、parent_id（直接子任务为 null）、agent（claude/codex）、title、status
（running/completed/blocked/failed）和 report_file。本轮 `subtasks/` 下每份 UTF-8 文件保存对应
子任务完整报告正文，不能只保存父任务摘要；不得使用绝对路径、越界路径或共用一个报告文件。
先原子保存全部报告和清单，再提交 receipt。DSH 读取清单及全部子报告后独立验收。
SDK 将全部实际轮次的 result.md、登记的子报告正文与可信 verdict 合并到 report 字段及 report.md；
缺失或未完成项会显示 report_warnings，不据此改写业务 verdict。此模式不改变单 worker 会话规则。

## 有界观察与判断

```bash
python3.13 <skill>/scripts/claude_session.py watch \
  --request /absolute/run/request.json --session dsh-claude-unique --wait-seconds 60
```

watch 每 10 秒检查一次，单次最多等待 300 秒，且所有命令受本轮剩余执行时间限制。
工具执行超时应覆盖实际等待和命令开销；不得借新 watch、恢复或返工延长 SDK 截止时间。
等待和文件锁每约 100ms 检查截止时间/停止标记。sdk-stop.json 出现后不得继续派发、选择或恢复；
SDK 已接管清理。runtime.json 的 monotonic 截止时间只在本轮同一 boot 内有效，不用于跨重启恢复。
同步调用，不转入长期后台 `job_output` 等待。每次返回后先检查 status、当前屏幕、快照和实际文件：

| status | 管理者下一步 |
|---|---|
| `starting` | 尚在启动或等待输入界面就绪，继续 watch，不重复 run/send |
| `running` | 检查返回的屏幕和 observation，任务继续则再次 watch；已停在输入框却无文件则诊断、补交 |
| `needs_attention` | 根据 reason、屏幕和 snapshot_file 决定观察、选择、恢复或结束 |
| `receipt_ready` | 有效 token 回执与结果文件已就绪，读取文件并独立验收；blocked 表示报告阻塞 |
| `stopped` | worker 已结束，记录失败证据并写 rejected verdict |

默认连续 300 秒没有计入活动的变化才触发 `silent`。spinner、计时器、重复错误、终端输出字节、
光标位置或显示模式变化都算活动；无需判断内容价值。仅客户端本地绘制的光标闪烁不可从 tmux
读取，不凭空产生活动。SDK waiting 心跳默认也计入；request 中
`sdk_heartbeat_counts_as_activity=false` 时才排除。持续心跳会阻止静默超时，仍须检查每次
watch 到期返回的屏幕；不能因 SDK 还活着就认定 worker 正常。整个任务仍受 SDK 总时限约束。

菜单、错误提示、无效回执、worker 退出或监测管道失效会提前返回。错误匹配只是诊断线索，
不自动判失败。历史错误若已恢复，使用 `watch --acknowledge <id>` 继续观察；静默时间不清零。
若静默时有证据表明长命令正常执行，同样可 acknowledge，在本次 watch 时段内暂缓再次提醒。

## 主动恢复与选择

```bash
python3.13 <skill>/scripts/claude_session.py recover \
  --request /absolute/run/request.json --session dsh-claude-unique \
  --observation 3 --reason '连接已恢复，继续当前任务'

python3.13 <skill>/scripts/claude_session.py choose \
  --request /absolute/run/request.json --session dsh-claude-unique \
  --observation 4 --keys Down Enter --reason '选择符合任务要求的选项'
```

recover 默认发送“继续当前任务”及原任务交接协议，也可用 `--instruction-file` 提供补齐文件的
具体要求。仍在忙时不能盲目堆积输入；有证据需要中断才能恢复时显式加 `--interrupt`。
恢复沿用当前 attempt/token，不得跳到新一轮或伪造回执。无效回执先归档，由 worker 重写。
若有效回执已到达，控制器拒绝恢复，转入验收。

DSH 代为判断交互选择，明确当前选项及理由，再发送按键；不能一律选 Yes。屏幕变化导致
observation 过期时重新 watch。Codex 的 `[Pasted Content N chars]` 表示可能尚未提交，可经
观察用 choose 发送 Enter。每次操作后都要观察是否生效。

主动恢复和反复卡住的菜单共享整个 run 的恢复预算，最多 5 次；首次普通菜单确认免费，
仅移动选项或勾选复选框不消耗恢复次数。
次数保存在 supervision.json，watch 重启及业务返工均不重置。预算用尽不得改文件绕过，必须结束。
网络/模型暂时故障可恢复；明确失败、无法修复的工具或凭据问题应结束，无需等待 receipt。

## 结束与排查

```bash
python3.13 <skill>/scripts/claude_session.py stop \
  --request /absolute/run/request.json --session dsh-claude-unique --reason '已无法恢复'
python3.13 <skill>/scripts/claude_session.py capture --session dsh-claude-unique --history-lines 50000
python3.13 <skill>/scripts/claude_session.py status --session dsh-claude-unique
```

stop 在剩余预算内先保存最多 50000 行历史，再停止会话，留下审计记录。失败时先 stop 再写 rejected verdict，
没有 receipt 不妨碍失败结论。正常验收后保留会话给 SDK 补采，再由 SDK 按 keep_session 配置清理；
只有 accepted 且 keep_session=true 才允许保留 worker。rejected、取消、失败和总超时必须停止
worker，即使 keep_session=true，诊断文件仍保留。SDK 会核验清理并返回 cleanup 报告；
cleanup_run/CLI cleanup 用于 SDK 结束后回收资源，不代表重新执行或重新验收任务。

capture 默认读取当前屏幕及最近 5000 行历史，`--history-lines 0` 只看当前屏幕；历史上限 50000。
Claude 的备用屏幕或原地重绘可能没有可恢复历史。长文完整性由 result.md 及引用文件保障，屏幕仅用于状态和诊断。
控制器 stdout 为 JSON、stderr 为诊断。屏幕文字、文件出现或静止不能替代 token 回执，更不能
替代 DSH 独立 verdict。监督状态、恢复次数与操作依据见 run 目录的 supervision.json、
supervision.jsonl 和 observations/。
