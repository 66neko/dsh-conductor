# AGENTS.md — 给 AI agent 的项目说明

本文件面向在此仓库工作的 AI agent。人类用户请看 [`README.md`](README.md)。

## 这个项目是什么

`dsh-conductor` 让 **DSH（DeepSeek Harness）** 充当编排器与验收方，把任务委派给
**Claude Code / Codex** 执行，并且**独立验证产物**后才把结论交回调用方。

它**不是**：

- 不是 agent 框架，不实现 LLM 调用、工具系统、会话持久化——那些都是 DSH 的职责
- 不是 tmux 的替代品，而是通过 `tmux-coding-agents` skill 使用 tmux
- 不替调用方决定用哪个 agent（`--agent` 由调用方指定，两个 agent 地位对等）

## 架构与数据流

```
调用方 (Python)          conductor/                  DSH 运行时              下级 agent
  cmd_run ──prompt──►  cli.py ──JSON-RPC/stdio──► dsh --profile sdk ──► 加载 skill
                          ▲                        (编排 + 独立验收)      │
                          │                              │                ▼
                          │                              └──bash──► agent_task.py
                          │                                              │
                          │                                              ▼
                          │                                        tmux 会话中的
                          │                                       Claude Code / Codex
                          │                                              │
                          └──── result.json（文件事实）◄─────────────────┘
```

三层职责必须保持清晰：

| 层 | 职责 | 不做什么 |
|---|---|---|
| `conductor/cli.py` | 参数解析、状态目录、驱动 DSH、读回结论 | 不解释模型输出、不判断工作质量 |
| DSH（外部） | 委派、**独立验收**、重试、落盘结论 | 不直接改产物 |
| `skills/tmux-coding-agents` | 启动/驱动交互式 TUI、读屏、发按键 | 不判断任务是否完成 |

## 关键不变量（改代码时不要破坏）

1. **stdout 保持机器可读。** 所有进度、日志、诊断一律走 **stderr**。
   调用方常常 `conductor run ... > result.txt`。
2. **完成判定只用事实，不用启发式。**
   - DSH 是否结束 → 协议事件 `turn/end.reason.kind` + `session.status == idle`
   - 下级 agent 是否结束 → skill 的 `--wait-file`（文件事实）优先于 `settle`
   - 验收是否通过 → `result.json` 是否存在且 `status == "accepted"`
   绝不解析模型的自然语言回复来判断成败。
3. **skill 只有一份源**，在 `skills/tmux-coding-agents/`。
   `conductor install-skill` 把它软链到 `~/.dsh/skills/`，**不要复制**成两份。
4. **结论落盘后调用方才认。** `cli.py` 会在运行前删掉旧的 `result.json`，
   所以它的存在必然代表本次运行产生了它——不要改成从 stdout 解析结论。
5. **`DSH_PERMISSION_MODE` 必须是 `danger-full-access`。** DSH 要靠它的 `bash` 工具
   执行 `agent_task.py` 和验证命令；confining 沙箱在缺少 `bwrap`/`landlock` 的机器上
   会**整体拒绝** bash（fail-closed），工作流会卡在第一步。

## 常用命令

```bash
python3 -m conductor doctor            # 环境自检（先跑这个）
python3 -m conductor install-skill     # 把 skill 软链到 ~/.dsh/skills/
python3 -m conductor run --workspace /tmp/w --task "创建 a.txt" --verify "a.txt 存在"
python3 -m conductor --help

python3 -m py_compile conductor/*.py   # 语法自检（本项目无测试框架依赖）
```

## 改代码时的注意事项

| 想改什么 | 改哪里 |
|---|---|
| 编排/验收的流程与话术 | `conductor/prompt.py` 的 `PROMPT_TEMPLATE` |
| 支持的 agent、启动参数 | `skills/tmux-coding-agents/scripts/tmux_agent.py` 的 `AGENT_SPECS` |
| 弹窗识别规则（光标符号/提示语） | 同上，`_CURSOR_GLYPHS` / `_MENU_HINT` |
| DSH 进度显示 | `conductor/progress.py` |
| 下级 agent 日志来源 | `conductor/transcript.py` |
| CLI 参数 | `conductor/cli.py` 的 `build_parser()` |

改 skill 时**务必同步 `skills/tmux-coding-agents/SKILL.md`**——那是模型实际读到的说明，
代码与文档不一致会让模型用错 API。

## 依赖与环境

- Python 3.8+（**零第三方依赖**，全部标准库）
- `tmux`
- **DSH — DeepSeek Harness**：<https://github.com/deepseek-ai/deepseek-harness>
- 下级 agent：Claude Code 和/或 Codex（至少一个）

## 已知边界

- `result.json` 的可信度 = DSH 验收的质量。`--verify` 写得越具体越可靠。
- 两层模型调用，小任务约 40–60 秒。
- 全屏 TUI 的滚出内容不在 tmux 滚动缓冲里（备用屏幕无历史），
  所以完整日志取自 agent 自己的 JSONL 转录，不是抓屏。详见 `docs/architecture.md`。
