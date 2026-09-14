"""由 DSH 管理运行在 tmux 中的编码 agent。"""

from .dsh import DshClient, DshConfig, DshError, RunResult
from .models import AgentKind, ExecutionPlan, Verdict
from .progress import RunEvent
from .sdk import Conductor, ConductorConfig, ConductorError, TaskResult

__version__ = "0.3.1"

__all__ = [
    "AgentKind",
    "Conductor",
    "ConductorConfig",
    "ConductorError",
    "DshClient",
    "DshConfig",
    "DshError",
    "RunResult",
    "RunEvent",
    "TaskResult",
    "ExecutionPlan",
    "Verdict",
    "__version__",
]
