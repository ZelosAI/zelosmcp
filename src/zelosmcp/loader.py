"""Minimal subagent-metadata stub (#24).

The full skill/hook/agent loader is the v0.4 follow-up (A.4); for v0.3 the
sync-subagent tools (#24) only need a small static roster so each subagent can
be exposed as an MCP tool with a stable name + description.

The roster intentionally matches the issue's "initial subagent roster":
``Plan``, ``Explore``, ``general-purpose``. Custom org subagents are deferred
to A.4, where this stub is replaced by a store-backed loader that reads the
asset framework.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SubagentMeta:
    """Metadata describing one subagent exposed as a sync MCP tool.

    ``name`` is the broker-facing subagent type (sent in the sync-channel
    ``open`` request and echoed in the staged ``open`` frame). ``tool_name`` is
    the MCP tool name surfaced to the IDE. ``description`` is the tool
    description.
    """

    name: str
    tool_name: str
    description: str


# v0.3 static roster. Tool names are lowercased + suffixed so they read as
# verbs in the IDE tool list (``plan`` / ``explore`` / ``general_purpose``).
_ROSTER: tuple[SubagentMeta, ...] = (
    SubagentMeta(
        name="Plan",
        tool_name="plan",
        description=(
            "Run the Plan subagent synchronously over a broker sync channel. "
            "Streams the subagent's turns (token deltas, tool calls, tool "
            "results) back to the IDE and returns the consolidated transcript. "
            "Use to produce an implementation plan for a task before editing."
        ),
    ),
    SubagentMeta(
        name="Explore",
        tool_name="explore",
        description=(
            "Run the Explore subagent synchronously over a broker sync "
            "channel. Streams turns back to the IDE and returns the "
            "consolidated transcript. Use to investigate a codebase / answer "
            "a research question across many files."
        ),
    ),
    SubagentMeta(
        name="general-purpose",
        tool_name="general_purpose",
        description=(
            "Run the general-purpose subagent synchronously over a broker "
            "sync channel. Streams turns back to the IDE and returns the "
            "consolidated transcript. Use for open-ended multi-step tasks."
        ),
    ),
)

_BY_TOOL_NAME: dict[str, SubagentMeta] = {m.tool_name: m for m in _ROSTER}
_BY_NAME: dict[str, SubagentMeta] = {m.name: m for m in _ROSTER}


def list_subagents() -> tuple[SubagentMeta, ...]:
    """Return the static v0.3 subagent roster."""
    return _ROSTER


def get_subagent(tool_name: str) -> SubagentMeta | None:
    """Look up a subagent by its MCP tool name (e.g. ``"plan"``)."""
    return _BY_TOOL_NAME.get(tool_name)


def get_subagent_by_name(name: str) -> SubagentMeta | None:
    """Look up a subagent by its broker subagent type (e.g. ``"Plan"``)."""
    return _BY_NAME.get(name)
