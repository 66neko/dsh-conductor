"""编排与验收的指令模板。

单独成文件是刻意的：**要让 DSH 换个方式干活，改这里就行**，不必碰 CLI 逻辑。
模板里的 `{...}` 占位符由 {@link build_prompt} 填充。

写这类 prompt 的三条经验（都是实测得来的）：

1. **必须明说"下级 agent 的自述不算证据"**。否则 DSH 容易把下级 agent 的
   "已完成" 直接转述成验收结论，验收就退化成橡皮图章。
2. **任务文本要走文件，不能拼进命令行**。任意自然语言里的引号、换行、`$`
   都会破坏 shell 命令；`--task "$(cat task.txt)"` 是安全的写法。
3. **必须指定结构化落盘**。结论写成文件才是事实——调用方靠这个文件判断成败，
   而不是去解析模型的自然语言回复。
"""

from __future__ import annotations

PROMPT_TEMPLATE = """你是任务编排器。严格按步骤执行，不要跳步。

# 总目标
让下级的编码 agent 在目录 `{workspace}` 中完成下面这个任务，**由你独立验收**，
确认真正完成后把结构化结果落盘。

## 用户任务
{task}

## 验收标准
{verify}

# 执行步骤

## 1. 委派给下级 agent
先加载 `tmux-coding-agents` skill（用 skill 工具，skill 名就是 `tmux-coding-agents`），
阅读它的说明，然后按它的方式启动下级 agent。任务文本已存在文件里，
**必须用文件读取而不是把内容拼进命令行**，以免引号/换行破坏命令：

```bash
TASK="$(cat {task_file})"
python3 {skill}/scripts/agent_task.py run --agent {agent_kind} --cwd {workspace} \\
    --task "$TASK" --session {session} --keep --timeout-ms 900000
```

`--keep` 会保留下级会话，便于验收不通过时追加指令。

## 2. 独立验收（关键步骤，不可省略）
下级 agent 的自述**不算证据**。你必须自己动手核实：
- 用 bash 的 `ls` 确认产物文件确实存在于 `{workspace}` 下
- 用 `read` 工具读取产物内容，逐条对照验收标准
- 尽可能做实际验证（例如检查 HTML 是否含必需标签、脚本是否真能运行）

## 3. 不通过则修正并重验
若验收不通过，向**同一个**下级会话追加具体修正要求，然后重新验收：

```bash
python3 {skill}/scripts/agent_task.py send --session {session} --text "具体问题与修正要求"
python3 {skill}/scripts/agent_task.py settle --session {session} --quiet-ms 4000
```

最多重试 {max_attempts} 轮。每轮都要重新执行步骤 2 的独立验收。

## 4. 落盘结构化结果（必须做，无论成败）
用 `write` 工具把结果写入 `{result_file}`，内容为**严格 JSON**（不要加注释、不要加代码块围栏）：

{{
  "status": "accepted",
  "task": "用户任务原文",
  "attempts": 1,
  "artifacts": ["产物文件相对于工作目录的路径"],
  "verification": "你实际执行了哪些验证命令、观察到什么具体结果",
  "claude_last_message": "下级 agent 的最后回复",
  "notes": "遗留问题或补充说明"
}}

`status` 取值规则：
- `"accepted"` — **仅当**你已独立验证产物确实符合验收标准
- `"rejected"` — 达到重试上限仍不符合，或根本无法验证

`attempts` 是实际委派轮次（整数）。`artifacts` 只列真实存在且已核实的文件。

## 5. 收尾
验收通过就关闭会话：

```bash
python3 {skill}/scripts/agent_task.py close --session {session}
```

最后用**一句话**总结：状态（accepted/rejected）、产物文件、验收结论。
"""


def build_prompt(
    *,
    workspace: str,
    task: str,
    verify: str,
    task_file: str,
    result_file: str,
    skill_dir: str,
    session: str,
    agent_kind: str,
    max_attempts: int,
) -> str:
    """填充编排 prompt。

    @param workspace - 下级 agent 的工作目录（绝对路径）
    @param task - 用户任务原文
    @param verify - 验收标准，越具体验收越可靠
    @param task_file - 任务文本落盘路径（供 DSH 用 `cat` 读取，避免命令行转义问题）
    @param result_file - DSH 必须写入的结构化结论路径
    @param skill_dir - `tmux-coding-agents` skill 所在目录
    @param session - tmux 会话名
    @param agent_kind - `claude` 或 `codex`
    @param max_attempts - 最多委派轮次
    @returns 完整的编排指令
    """
    return PROMPT_TEMPLATE.format(
        workspace=workspace,
        task=task,
        verify=verify,
        task_file=task_file,
        result_file=result_file,
        skill=skill_dir,
        session=session,
        agent_kind=agent_kind,
        max_attempts=max_attempts,
    )
