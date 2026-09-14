---
name: tmux-claude-code
description: 在 tmux 中启动并控制交互式 Claude Code 会话，提交首轮或后续编码任务，等待绑定 token 的交接回执，查看终端并关闭会话。用于必须保留在可 attach tmux 会话中的 Claude Code 委派任务；不要用于 Codex 会话。
---

# 在 tmux 中运行 Claude Code

通过 Python 3.13 运行 `scripts/claude_session.py`。控制器使用
`--dangerously-skip-permissions` 启动 Claude Code，处理 Claude 的启动菜单，并给 tmux 会话
写入 agent 标记，避免 Codex 控制器误连接到该会话。

## 提交任务

管理者会提供所有路径和 token。任务内容必须通过 `--task-file` 传入，不能把任务文本插入 shell
命令。控制器会让 worker 直接读取该文件，只向 TUI 提交短交接指令，避免长任务正文在交互式
编辑器中被截断。

```bash
python3.13 <skill>/scripts/claude_session.py run \
  --workspace /absolute/workspace \
  --session dsh-claude-unique \
  --task-file /absolute/attempt/task.md \
  --receipt /absolute/attempt/receipt.json \
  --token UNIQUE_TOKEN \
  --timeout-seconds 1200
```

`run` 只有在 worker 使用指定 token 写出合法回执后才返回。回执只表示 worker 已交回控制权，
不证明任务正确。接受任务前必须由管理者独立检查和测试工作区。

控制器会等待输入界面稳定，避免延迟出现的目录信任菜单截断任务。若提交后仍出现启动菜单，
控制器会处理菜单、清空残留输入并完整重投一次任务与交接指令。长文本粘贴完成后还会进行一次
有界补交，避免首个 Enter 早于编辑器完成展开。

需要返工时，将修正要求写入新的任务文件，并在同一个会话中使用新的回执路径和 token：

```bash
python3.13 <skill>/scripts/claude_session.py send \
  --session dsh-claude-unique \
  --task-file /absolute/attempt-2/task.md \
  --receipt /absolute/attempt-2/receipt.json \
  --token NEW_UNIQUE_TOKEN \
  --timeout-seconds 1200
```

## 观察与清理

```bash
python3.13 <skill>/scripts/claude_session.py status --session dsh-claude-unique
python3.13 <skill>/scripts/claude_session.py capture --session dsh-claude-unique
python3.13 <skill>/scripts/claude_session.py close --session dsh-claude-unique
```

控制器结果全部以 JSON 输出到 stdout，诊断信息输出到 stderr。超时或启动失败时，会话会保留
以便排查。可以使用 JSON 结果中的 `attach_command` 连接，并用 `Ctrl-b d` 脱离。

不要把终端画面稳定或 Claude 的自然语言回复当作完成信号。此 skill 暴露的唯一完成信号是
绑定 token 的回执文件。
