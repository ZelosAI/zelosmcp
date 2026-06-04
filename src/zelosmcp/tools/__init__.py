"""Data-path MCP tools (#24, #25).

Two tool families exposed by the data-path MCP server (see
:mod:`zelosmcp.server`):

* :mod:`zelosmcp.tools.sync_subagent` — one tool per subagent type (``Plan`` /
  ``Explore`` / ``general-purpose``). Each opens a broker sync channel, streams
  the subagent's turn frames, and returns the consolidated transcript (#24).
* :mod:`zelosmcp.tools.async_task` — ``submit_inference_task``: opens a broker
  share, publishes a backplane request envelope carrying the share coords, and
  returns ``{id, replyTopic}`` (#25).

Both families issue per-invocation bearer tokens (#26) for their downstream
calls and run on behalf of the gateway-propagated caller identity.
"""

from __future__ import annotations

from zelosmcp.tools.async_task import (
    ASYNC_TASK_TOOL,
    AsyncTaskDeps,
    submit_inference_task,
)
from zelosmcp.tools.sync_subagent import (
    SubagentDeps,
    SyncSubagentResult,
    run_sync_subagent,
    subagent_tool_specs,
)

__all__ = [
    "SubagentDeps",
    "SyncSubagentResult",
    "run_sync_subagent",
    "subagent_tool_specs",
    "AsyncTaskDeps",
    "ASYNC_TASK_TOOL",
    "submit_inference_task",
]
