# AGENTS.md

本项目让 DSH 管理运行在 tmux 中的 Claude Code 或 Codex，并在 DSH 独立验收后输出结构化结论。

## 分层

| 层 | 职责 |
|---|---|
| `conductor/cli.py` | CLI、运行目录、DSH 生命周期、verdict 结构校验、机器输出 |
| `conductor/dsh.py` | `dsh --profile sdk` 的 JSON-RPC/stdio 协议 |
| `conductor/agents/` | 两种 TUI 各自的启动、弹窗与就绪协议 |
| `conductor/tmux.py` | 无 agent 语义的 tmux 会话与字面文本传输 |
| `skills/tmux-claude-code` | DSH 使用的 Claude Code 操作说明与入口 |
| `skills/tmux-codex` | DSH 使用的 Codex 操作说明与入口 |
| DSH | 委派、独立验收、返工、写 verdict |

## 不变量

1. Python 最低版本是 3.13，代码只使用标准库。
2. `conductor run` 的 stdout 只有一个 JSON 对象；进度与诊断只能写 stderr。
3. agent 由调用方通过必填的 `--agent` 选择，验收标准通过必填的 `--verify` 提供。
4. worker 回合结束只认 token-bound `receipt.json`。不解析回复，不使用屏幕静止或产物出现判断结束。
5. receipt 不是验收证据。DSH 必须亲自读产物并运行验证，worker 不能修改 verdict。
6. 成功需要两个事实同时成立：DSH 协议报告 `turn/end.reason.kind == completed` 且 session idle；
   唯一 run 目录中的 verdict 通过结构与身份校验且 `status == accepted`。
7. `DSH_PERMISSION_MODE` 固定为 `danger-full-access`，DSH 需要 bash 执行 skill 和验收命令。
8. 两个 skill 只有仓库中的源目录；安装操作只创建软链。
9. 修改任一 agent 的参数、菜单或命令接口时，同步其 `SKILL.md` 并运行 skill validator。
10. tmux 文本通过 `load-buffer`/`paste-buffer` 传递，不能把自然语言拼进 shell 命令。

## 常用命令

```bash
python3.13 -m conductor install-skills
python3.13 -m conductor doctor
python3.13 -m conductor run --workspace /tmp/work --agent claude --task "..." --verify "..."
python3.13 -m unittest discover -v
python3.13 -m compileall -q conductor skills tests
```
