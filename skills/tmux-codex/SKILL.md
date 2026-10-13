---
name: tmux-codex
description: 在 tmux 中监督 Codex，提交任务、观察活动与回执、恢复连接故障、代做交互选择并保存失败证据。
---

# 在 tmux 中监督 Codex

通过 Python 3.13 运行 `scripts/codex_session.py`，使用 `--dangerously-bypass-approvals-and-sandbox --no-alt-screen` 启动 worker。
只控制 request/plan 指定的本 agent 会话和工作区，所有返工沿用该会话。
会话名精确匹配，读屏与输入绑定启动时的 worker pane。

本轮使用 SDK 创建的私有 tmux socket，run/send/watch/recover/choose/stop 根据 --request
读取同目录 runtime.json。status/capture/close 使用注入的 DSH_CONDUCTOR_SOCKET。

## 委派与交接

```bash
python3.13 <skill>/scripts/codex_session.py run \
  --request /absolute/run/request.json \
  --session dsh-codex-unique \
  --task-file /absolute/attempt/task.md \
  --receipt /absolute/attempt/receipt.json \
  --token UNIQUE_TOKEN
```

`run` 立即登记任务并返回 `starting`；随后必须 `watch`，由 watch 在输入界面就绪后提交任务。
只有收到有效回执且独立验收后需要业务返工，才使用 `send`，参数与 run 相同，但取下一轮的文件和新 token。

每轮 worker 必须将完整回答、关键结论、修改记录、检查结果、阻塞信息写入 receipt 同目录的
非空 UTF-8 `result.md`。重要内容及时落盘，长检查输出另存文件并在报告中引用。最终报告先写
临时文件再原子替换，然后运行控制器提供的 `complete` 命令。终端只显示简短进度和路径。
收到回执后，DSH 必须读完整报告及相关引用文件，再独立验收工作区。

## 有界观察与判断

```bash
python3.13 <skill>/scripts/codex_session.py watch \
  --request /absolute/run/request.json --session dsh-codex-unique --wait-seconds 60
```

watch 每 10 秒检查一次，单次最多等待 300 秒，且所有命令受本轮剩余执行时间限制。
sdk-stop.json 出现后不得继续派发、选择或恢复。同步调用，不转入长期后台等待。

| status | 管理者下一步 |
|---|---|
| `starting` | 尚在启动或等待输入界面就绪，继续 watch |
| `submitting` | 已开始粘贴，尚未确认 Codex 接收；继续 watch |
| `running` | 已确认提交，检查屏幕和 observation |
| `reconnecting` | Codex 正在自动重连，继续 watch，等待自动重试结束 |
| `needs_attention` | 根据 reason、屏幕和 snapshot_file 决定观察、选择、恢复或结束 |
| `receipt_ready` | 有效 token 回执与结果文件已就绪，读取文件并独立验收 |
| `stopped` | worker 已结束，记录失败证据并写 rejected verdict |

提交确认与 300 秒活动静默计时独立。控制器使用 bracketed paste，先观察文字进入输入框，
再单独发送 Enter。粘贴开始后 30 秒仍不能确认时返回 `submission_unconfirmed`。
Enter 后草稿仍在时，下次采样提前返回 `submission_pending`。

遇到 `submission_pending`，查看当前屏幕，用 `choose --keys Enter` 补发一次提交；
普通提交的每次补发计入全 run 最多 5 次恢复预算，reconnect 的提交补发除外。
草稿还在时禁止 recover 追加"继续"或重新粘贴任务。

遇到 `submission_unconfirmed`，先检查快照和当前屏幕；只有确认输入框为空且未在忙时才 recover 重试，
必要时有依据地 interrupt。无法确定或恢复耗尽则 stop。

默认连续 300 秒没有计入活动的变化才触发 `silent`。spinner、计时器、重复错误、终端输出字节、
光标位置或显示模式变化都算活动。SDK waiting 心跳默认也计入；request 中
`sdk_heartbeat_counts_as_activity=false` 时才排除。

Codex 执行中通常显示 `• Working (1m 05s • esc to interrupt)`。worker-screen.log 会保留其中
的实际计时，计时变化视为 worker 活动。

菜单、错误提示、无效回执、worker 退出或监测管道失效会提前返回。历史错误若已恢复，
使用 `watch --acknowledge <id>` 继续观察。

## 自动重连与重试耗尽

出现 `Reconnecting... N/M`、`Reconnecting…` 或 `Reconnecting to server…` 时，控制器返回
`reconnecting`。N 为当前重试次数，M 为用户配置的总次数，可为任意数值。自动重连期间的
网络错误和活动静默不触发错误恢复。继续 watch；即使 N 已等于 M，只要仍显示自动重连，
也必须等这次重试结束，不发送 recover、选择按键或中断。是否耗尽依据连接终态错误判断，
不依据固定次数，也不单凭 N/M 判断。

自动重试结束后仍有 `exceeded retry limit` 或 `stream disconnected before completion` 等
连接终态错误时，watch 返回 `needs_attention` / `reconnect_required`。确认输入框空且 worker
已停止执行后，使用专用重连命令：

```bash
python3.13 <skill>/scripts/codex_session.py reconnect \
  --request /absolute/run/request.json --session dsh-codex-unique \
  --observation 5 --reason 'Codex 自动重试耗尽，重新连接并继续原任务'
```

reconnect 通过原会话发送继续当前任务的指令及原交接协议，沿用 attempt/token，并通过
watch 确认输入已提交；它不是 Codex 的 slash 命令。每次操作后必须重新 watch，不连续堆积
指令。重连次数单独持久化为 `reconnects`，不扣 `recoveries`，重连提交需要补发 Enter 时也
不扣错误恢复次数。即使错误恢复预算已耗尽，也可在确认自动重试耗尽后 reconnect。
自动等待和手动重连仍受整个 run 的截止时间、取消和 sdk-stop.json 限制。

## 主动恢复与选择

```bash
python3.13 <skill>/scripts/codex_session.py recover \
  --request /absolute/run/request.json --session dsh-codex-unique \
  --observation 3 --reason '连接已恢复，继续当前任务'

python3.13 <skill>/scripts/codex_session.py choose \
  --request /absolute/run/request.json --session dsh-codex-unique \
  --observation 4 --keys Down Enter --reason '选择符合任务要求的选项'
```

recover 默认发送"继续当前任务"及原任务交接协议，也可用 `--instruction-file` 提供补齐文件的
具体要求。有证据需要中断才能恢复时显式加 `--interrupt`。恢复沿用当前 attempt/token。

DSH 代为判断交互选择，明确当前选项及理由，再发送按键。每次操作后都要观察是否生效。

主动恢复、补发提交 Enter 和反复卡住的菜单共享整个 run 的恢复预算，最多 5 次；首次普通菜单确认免费。
Codex 重连及其提交补发除外。次数保存在 supervision.json，watch 重启及业务返工均不重置。

## 结束与排查

```bash
python3.13 <skill>/scripts/codex_session.py stop \
  --request /absolute/run/request.json --session dsh-codex-unique --reason '已无法恢复'
python3.13 <skill>/scripts/codex_session.py capture --session dsh-codex-unique --history-lines 50000
python3.13 <skill>/scripts/codex_session.py status --session dsh-codex-unique
```

stop 在剩余预算内先保存最多 50000 行历史，再停止会话，留下审计记录。失败时先 stop 再写 rejected verdict。
正常验收后保留会话给 SDK 补采，再由 SDK 按 keep_session 配置清理。

capture 默认读取当前屏幕及最近 5000 行历史，`--history-lines 0` 只看当前屏幕；历史上限 50000。
Codex 的 --no-alt-screen 有助于保留历史，但重绘与历史容量仍可能造成遗漏。
控制器 stdout 为 JSON、stderr 为诊断。监督状态、恢复次数与操作依据见 run 目录的 supervision.json、
supervision.jsonl 和 observations/。
