"""由 DSH 管理运行在 tmux 中的编码 agent。"""

from .dsh import DshClient, DshConfig, DshError, RunResult
from .models import AgentKind, Verdict

__version__ = "0.2.0"

__all__ = [
    "AgentKind",
    "DshClient",
    "DshConfig",
    "DshError",
    "RunResult",
    "Verdict",
    "__version__",
]
