"""Agent runtime state: the task queue an out-of-process agent runtime works from, and
the executor that hands agent rules to an `AgentRuntime` (DESIGN.md 5.2, 5.5; D35). The
roles and skills, as data, are in `chipgraph.core.runtime.roles` (DESIGN.md 5.1; D8).

`chipgraph.core.runtime` is generic: it knows nothing about any agent harness, model or
tool. Runtimes live in `chipgraph.adapters.runtime`.
"""

from chipgraph.core.runtime.executor import (
    AgentRuntimeExecutor,
    instance_inputs_hash,
    outcome_for,
    task_for,
)
from chipgraph.core.runtime.queue import (
    ACTIVE_STATUSES,
    DEFAULT_TOOL_CALL_CAP,
    OPEN_STATUSES,
    AgentBinding,
    AgentTaskRecord,
    QueueError,
    TaskQueue,
    TaskStatus,
    state_key,
)

__all__ = [
    "ACTIVE_STATUSES",
    "DEFAULT_TOOL_CALL_CAP",
    "OPEN_STATUSES",
    "AgentBinding",
    "AgentRuntimeExecutor",
    "AgentTaskRecord",
    "QueueError",
    "TaskQueue",
    "TaskStatus",
    "instance_inputs_hash",
    "outcome_for",
    "state_key",
    "task_for",
]
