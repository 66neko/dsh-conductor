---
name: tmux-claude-code
description: 在 tmux 中监督 Claude Code，提交任务、观察活动与回执、恢复连接故障、代做交互选择并保存失败证据。
---

# 在 tmux 中监督 Claude Code

通过 Python 3.13 运行 `scripts/claude_session.py`，使用 `--dangerously-skip-permissions` 启动 worker。
只控制 request/plan 指定的本 agent 会话和工作区，所有返工沿用该会话。
会话名精确匹配，读屏与输入绑定启动时的 worker pane。

本轮使用 SDK 创建的私有 tmux socket，run/send/watch/recover/choose/stop 根据 --request
读取同目录 runtime.json。status/capture/close 使用注入的 DSH_CONDUCTOR_SOCKET。

## 委派与交接

```bash
python3.13 <skill>/scripts/claude_session.py run \
  --request /absolute/run/request.json \
  --session dsh-claude-unique \
  --task-file /absolute/attempt/task.md \
  --receipt /absolute/attempt/receipt.json \
  --token UNIQUE_TOKEN
```

`run` 立即登记任务并返回 `starting`；随后必须 `watch`，由 watch 在输入界面就绪后提交任务。
只有收到有效回执且报告验收不通过需要返工，才使用 `send`，参数与 run 相同，但取下一轮的文件和新 token。

每轮 worker 必须将结论、修改文件、检查结果、未完成事项或阻塞信息精炼写入 receipt 同目录的
非空 UTF-8 `result.md`，尽量控制在 60 行内，返工轮只写本轮变化。完整日志、长检查输出和长答案
另存文件并在报告中引用路径。最终报告先写
临时文件再原子替换，然后运行控制器提供的 `complete` 命令。终端只显示简短进度和路径。
收到回执后，DSH 完整读取 result.md，只判断报告是否完成用户 prompt 的全部目标，不复核工作区。

## 有界观察与判断

```bash
python3.13 <skill>/scripts/claude_session.py watch \
  --request /absolute/run/request.json --session dsh-claude-unique --wait-seconds 60
```

watch 每 10 秒检查一次，单次最多等待 300 秒，且所有命令受本轮剩余执行时间限制。
sdk-stop.json 出现后不得继续派发、选择或恢复。同步调用，不转入长期后台等待。

| status | 管理者下一步 |
|---|---|
| `starting` | 尚在启动或等待输入界面就绪，继续 watch |
| `running` | 检查屏幕和 observation，任务继续则再次 watch |
| `needs_attention` | 根据 reason、屏幕和 snapshot_file 决定观察、选择、恢复或结束 |
| `receipt_ready` | 有效 token 回执与结果文件已就绪，读取 result.md 并做报告验收 |
| `stopped` | worker 已结束，记录失败证据并写 rejected verdict |

默认连续 300 秒没有计入活动的变化才触发 `silent`。spinner、计时器、重复错误、终端输出字节、
光标位置或显示模式变化都算活动。SDK waiting 心跳默认也计入；request 中
`sdk_heartbeat_counts_as_activity=false` 时才排除。

菜单、错误提示、无效回执、worker 退出或监测管道失效会提前返回。历史错误若已恢复，
使用 `watch --acknowledge <id>` 继续观察。

## 主动恢复与选择

```bash
python3.13 <skill>/scripts/claude_session.py recover \
  --request /absolute/run/request.json --session dsh-claude-unique \
  --observation 3 --reason '连接已恢复，继续当前任务'

python3.13 <skill>/scripts/claude_session.py choose \
  --request /absolute/run/request.json --session dsh-claude-unique \
  --observation 4 --keys Down Enter --reason '选择符合任务要求的选项'
```

recover 默认发送"继续当前任务"及原任务交接协议，也可用 `--instruction-file` 提供补齐文件的
具体要求。有证据需要中断才能恢复时显式加 `--interrupt`。恢复沿用当前 attempt/token。

DSH 代为判断交互选择，明确当前选项及理由，再发送按键。每次操作后都要观察是否生效。

主动恢复和反复卡住的菜单共享整个 run 的恢复预算，最多 5 次；首次普通菜单确认免费。
次数保存在 supervision.json，watch 重启及业务返工均不重置。

## 结束与排查

```bash
python3.13 <skill>/scripts/claude_session.py stop \
  --request /absolute/run/request.json --session dsh-claude-unique --reason '已无法恢复'
python3.13 <skill>/scripts/claude_session.py capture --session dsh-claude-unique --history-lines 50000
python3.13 <skill>/scripts/claude_session.py status --session dsh-claude-unique
```

stop 在剩余预算内先保存最多 50000 行历史，再停止会话，留下审计记录。失败时先 stop 再写 rejected verdict。
正常验收后保留会话给 SDK 补采，再由 SDK 按 keep_session 配置清理。

capture 默认读取当前屏幕及最近 5000 行历史，`--history-lines 0` 只看当前屏幕；历史上限 50000。
控制器 stdout 为 JSON、stderr 为诊断。监督状态、恢复次数与操作依据见 run 目录的 supervision.json、
supervision.jsonl 和 observations/。
