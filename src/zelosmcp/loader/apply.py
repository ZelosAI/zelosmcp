"""Apply a resolved bundle to a subagent invocation (#27).

:func:`apply_bundle` turns a :class:`~zelosmcp.loader.store.SubagentBundle`
into an :class:`AppliedBundle` — the concrete things handed to the subagent at
spawn:

* **env** — environment variables the subagent runtime sets (the manifest's
  ``env`` block verbatim).
* **system_prompt_fragments** — one fragment per skill, built from the skill's
  ``description`` + ``body``. These are injected ahead of the subagent's first
  turn so the skill's knowledge is in context (and, per the issue's manual
  check, the skill's ``description`` becomes available to the subagent).
* **hook_tools** — per-bundle hook references (``event`` → ``command``) the
  subagent registers as runtime hooks.

The :meth:`AppliedBundle.open_payload` method renders the compact,
broker-relayable view that rides in the ``POST /sync/channels`` request body
and is echoed into the staged ``open`` frame's subagent payload — i.e. the
"bundle reference" the issue calls for. Full skill bodies stay in
``system_prompt_fragments`` (delivered as the first turn's context) rather than
bloating the open frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zelosmcp.loader.store import HookArtifact, SkillArtifact, SubagentBundle


def _skill_fragment(skill: SkillArtifact) -> str:
    """Render one skill as a system-prompt fragment.

    Leads with the description (so it is unmissable in context) then appends
    the body. Mirrors the SKILL.md shape the asset framework pushes to IDEs so
    a subagent sees the same content whether it was pushed or loaded here.
    """
    header = f"# Skill: {skill.name}"
    lines = [header]
    if skill.description:
        lines.append(skill.description)
    if skill.body.strip():
        lines.append("")
        lines.append(skill.body.rstrip())
    return "\n".join(lines)


@dataclass
class AppliedBundle:
    """The concrete artifacts to hand a subagent at spawn.

    Built by :func:`apply_bundle`. ``open_payload`` produces the compact view
    relayed over the broker; the full ``system_prompt_fragments`` are delivered
    as first-turn context.
    """

    subagent: str
    env: dict[str, str] = field(default_factory=dict)
    system_prompt_fragments: list[str] = field(default_factory=list)
    hook_tools: list[dict[str, Any]] = field(default_factory=list)
    skill_refs: list[dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        """True when nothing would be delivered to the subagent."""
        return not (
            self.env
            or self.system_prompt_fragments
            or self.hook_tools
            or self.skill_refs
        )

    def open_payload(self) -> dict[str, Any]:
        """Compact bundle reference for the broker ``open`` frame's payload.

        Only the references (skill names + descriptions, hook event/command,
        env keys) travel here — the broker relays this verbatim into the staged
        ``open`` frame so the subagent knows which artifacts are active. Full
        skill bodies are delivered separately as first-turn context.
        """
        payload: dict[str, Any] = {}
        if self.skill_refs:
            payload["skills"] = list(self.skill_refs)
        if self.hook_tools:
            payload["hooks"] = list(self.hook_tools)
        if self.env:
            # Keys only — values may be secrets and the open frame is relayed.
            payload["env"] = sorted(self.env)
        return payload

    def system_prompt(self) -> str:
        """Concatenate the skill fragments into one system-prompt block."""
        return "\n\n".join(self.system_prompt_fragments)


def apply_bundle(bundle: SubagentBundle | None) -> AppliedBundle | None:
    """Assemble an :class:`AppliedBundle` from a resolved bundle.

    Returns ``None`` for ``None`` / empty input so callers can cheaply treat
    "no bundle" as the fixed-roster bare-launch path.
    """
    if bundle is None or bundle.is_empty():
        return None

    fragments = [_skill_fragment(s) for s in bundle.skills]
    skill_refs = [s.reference() for s in bundle.skills]
    hook_tools = [_hook_tool(h) for h in bundle.hooks]

    return AppliedBundle(
        subagent=bundle.subagent,
        env=dict(bundle.env),
        system_prompt_fragments=fragments,
        hook_tools=hook_tools,
        skill_refs=skill_refs,
    )


def _hook_tool(hook: HookArtifact) -> dict[str, Any]:
    """Render one hook as a per-bundle tool reference for the subagent."""
    return hook.reference()
