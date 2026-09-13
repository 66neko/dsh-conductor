"""dsh-conductor — 让 DSH 编排下级编码 agent 并独立验收。

三层结构：

    Python 脚本  ──JSON-RPC/stdio──►  DSH (DeepSeek Harness)  ──tmux──►  Claude Code / Codex
         ▲                                  │                                │
         └──────── result.json ─────────────┘◄──── 验收不通过则追加指令 ───────┘

对外只暴露四样东西：{@link DshClient}（驱动 DSH）、{@link ProgressReporter}
（DSH 实时进度）、{@link AgentTranscript}（下级 agent 的完整日志）、
{@link build_prompt}（编排与验收的指令模板）。
"""

from .dsh import DshClient, DshConfig, DshError, RunResult
from .progress import AgentFollower, ProgressReporter, summarize_tool_call
from .transcript import AgentTranscript, locate_transcript
from .prompt import build_prompt

__version__ = "0.1.0"

__all__ = [
    "AgentFollower",
    "AgentTranscript",
    "DshClient",
    "DshConfig",
    "DshError",
    "ProgressReporter",
    "RunResult",
    "build_prompt",
    "locate_transcript",
    "summarize_tool_call",
    "__version__",
]
