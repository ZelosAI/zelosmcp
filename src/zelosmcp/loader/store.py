"""Filesystem-backed subagent artifact store (#27).

At invoke time a sync-subagent tool needs the *bundle* of skills + hooks that
should be loaded into the subagent's runtime. This module reads those
artifacts from a local directory tree rooted at ``$ZELOS_ARTIFACTS_DIR``:

.. code-block:: text

    $ZELOS_ARTIFACTS_DIR/
      skills/<name>/SKILL.md      # an Agent Skill (frontmatter + body)
      hooks/<name>.json           # an agent hook entry (event → command)
      bundles/<subagent>.yaml     # which skills + hooks each subagent gets

The artifact *bodies* reuse the zelosMCP asset framework's wire shapes
(``framework/assetstore/kinds/{skill,hook}``): a skill is a SKILL.md with
``name`` / ``description`` / ``paths`` frontmatter; a hook is the
``{event, command}`` JSON entry. The framework owns *authoring* (the GUI /
YAML editor / seeder) and *pushing to IDEs*; this store is the read-only
*invoke-time* path that pulls the already-authored artifacts off disk and
hands them to a subagent over the broker.

Design (per the issue's Decisions):

* **Local filesystem store for EA.** An S3-backed store is a v1.0 item — the
  :class:`ArtifactStore` API is deliberately narrow (``load_bundle``) so it
  can be swapped without touching :mod:`zelosmcp.loader.apply`.
* **YAML manifests, not JSON.** Easier for humans to author. Parsed +
  validated through Pydantic models so a malformed manifest fails fast with a
  readable error (:class:`BundleManifestError`) *before* the broker channel is
  opened — never a runtime exception mid-turn.
* **No hot reload.** A bundle is read once per invocation; mid-session changes
  to disk require a re-invoke.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger("zelosmcp.loader.store")

# Env var naming the artifacts root. Honors the suite ``*_FILE`` convention
# only for secrets — this is a directory path, so a plain env var (or the
# default) is the contract.
ARTIFACTS_DIR_ENV = "ZELOS_ARTIFACTS_DIR"

# Default artifacts root when the env var is unset: a sibling of the SQLite
# state dir so an operator that mounts one PVC gets both. Resolved lazily so
# tests can point at a tmp dir purely via the env var.
_DEFAULT_SUBDIR = "artifacts"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


class BundleManifestError(ValueError):
    """Raised when a bundle manifest is missing, malformed, or invalid.

    Carries a human-readable message naming the offending file + field so the
    failure surfaces *before* the broker channel opens rather than as an opaque
    runtime exception mid-turn (the issue's manifest-validation requirement).
    """


# ── Parsed-artifact models ────────────────────────────────────────────────


class SkillArtifact(BaseModel):
    """One skill loaded from ``skills/<name>/SKILL.md``.

    ``description`` is the frontmatter line a subagent surfaces to decide
    whether to load the skill (the issue's manual-verification anchor:
    "observe the skill's ``description`` becomes available to the subagent").
    ``body`` is the full markdown the subagent reads once it engages the skill.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    paths: list[str] = Field(default_factory=list)
    body: str = ""

    def reference(self) -> dict[str, Any]:
        """Compact, broker-relayable view (no full body) for the open frame."""
        ref: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.paths:
            ref["paths"] = list(self.paths)
        return ref


class HookArtifact(BaseModel):
    """One hook loaded from ``hooks/<name>.json``.

    Mirrors the asset framework's hook entry (``event`` → ``command``). The
    subagent's runtime registers the command to run on the named event.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    event: str
    command: str

    @field_validator("event", "command")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("must be a non-empty string")
        return v

    def reference(self) -> dict[str, Any]:
        """Compact, broker-relayable view for the open frame."""
        return {"name": self.name, "event": self.event, "command": self.command}


class _BundleManifestModel(BaseModel):
    """Validated shape of a ``bundles/<subagent>.yaml`` manifest.

    ``additionalProperties: false`` (``extra="forbid"``) so a typo like
    ``skils:`` is a hard error, not a silent no-op.
    """

    model_config = ConfigDict(extra="forbid")

    subagent: str
    skills: list[str] = Field(default_factory=list)
    hooks: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("subagent")
    @classmethod
    def _subagent_non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("must be a non-empty string")
        return v


class SubagentBundle(BaseModel):
    """The fully-resolved bundle for one subagent invocation.

    Produced by :meth:`ArtifactStore.load_bundle`: the manifest's referenced
    skills + hooks loaded off disk, plus the manifest's ``env`` overrides.
    Consumed by :func:`zelosmcp.loader.apply.apply_bundle`.
    """

    model_config = ConfigDict(extra="forbid")

    subagent: str
    skills: list[SkillArtifact] = Field(default_factory=list)
    hooks: list[HookArtifact] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        """True when nothing would be applied (no skills / hooks / env)."""
        return not (self.skills or self.hooks or self.env)


# ── Store ──────────────────────────────────────────────────────────────────


def resolve_artifacts_dir() -> Path:
    """Return the artifacts root directory.

    ``$ZELOS_ARTIFACTS_DIR`` wins; otherwise an ``artifacts`` subdirectory of
    the suite state dir (so a single mounted PVC carries both the SQLite stores
    and the artifacts). Resolution is lazy — the directory need not exist; a
    missing root simply yields no bundle.
    """
    explicit = os.environ.get(ARTIFACTS_DIR_ENV)
    if explicit:
        return Path(explicit)
    # Import here to avoid a hard dependency on the state-dir module for the
    # common explicit-env-var path.
    from zelosmcp.framework.state_dir import resolve_state_dir

    return resolve_state_dir() / _DEFAULT_SUBDIR


def _parse_skill_md(text: str, *, fallback_name: str, source: Path) -> SkillArtifact:
    """Parse a SKILL.md (YAML frontmatter + markdown body) into a skill.

    A missing / malformed frontmatter block is tolerated: the whole file
    becomes the body and the directory name is the skill name with an empty
    description. A frontmatter block that is not a mapping is a hard error.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return SkillArtifact(name=fallback_name, body=text)

    fm_text, body = match.group(1), match.group(2)
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        raise BundleManifestError(
            f"skill {source}: frontmatter is not valid YAML: {exc}"
        ) from exc
    if not isinstance(fm, dict):
        raise BundleManifestError(
            f"skill {source}: frontmatter must be a YAML mapping"
        )

    paths = fm.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise BundleManifestError(
            f"skill {source}: 'paths' must be a string or list of strings"
        )

    return SkillArtifact(
        name=str(fm.get("name") or fallback_name),
        description=str(fm.get("description") or ""),
        paths=list(paths),
        body=body,
    )


class ArtifactStore:
    """Read-only, filesystem-backed loader for subagent artifact bundles.

    Construct with an explicit ``root`` or let it default to
    :func:`resolve_artifacts_dir`. The single public operation,
    :meth:`load_bundle`, reads a subagent's manifest and resolves it to a
    :class:`SubagentBundle`. Everything is read fresh on each call (no caching)
    so the "no hot reload, read once per invocation" contract holds without a
    process restart.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root) if root is not None else resolve_artifacts_dir()

    # ── Paths ──────────────────────────────────────────────────────────

    def _bundle_path(self, subagent: str) -> Path:
        return self.root / "bundles" / f"{subagent}.yaml"

    def _skill_path(self, name: str) -> Path:
        return self.root / "skills" / name / "SKILL.md"

    def _hook_path(self, name: str) -> Path:
        return self.root / "hooks" / f"{name}.json"

    # ── Public API ─────────────────────────────────────────────────────

    def has_bundle(self, subagent: str) -> bool:
        """True when a manifest exists for ``subagent``."""
        return self._bundle_path(subagent).is_file()

    def load_bundle(self, subagent: str) -> SubagentBundle | None:
        """Load + resolve ``subagent``'s bundle, or ``None`` if it has none.

        Returns ``None`` when the subagent has no manifest (the fixed-roster
        subagents launch bare, exactly as before #27). Raises
        :class:`BundleManifestError` when a manifest exists but is malformed or
        references a missing / invalid skill or hook — fail-fast, before the
        broker channel opens.
        """
        path = self._bundle_path(subagent)
        if not path.is_file():
            return None

        manifest = self._read_manifest(subagent, path)
        skills = [self._load_skill(name, path) for name in manifest.skills]
        hooks = [self._load_hook(name, path) for name in manifest.hooks]
        return SubagentBundle(
            subagent=manifest.subagent,
            skills=skills,
            hooks=hooks,
            env=dict(manifest.env),
        )

    # ── Loaders ────────────────────────────────────────────────────────

    def _read_manifest(self, subagent: str, path: Path) -> _BundleManifestModel:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BundleManifestError(f"bundle manifest {path}: cannot read: {exc}") from exc
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise BundleManifestError(
                f"bundle manifest {path}: not valid YAML: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise BundleManifestError(
                f"bundle manifest {path}: document must be a YAML mapping"
            )
        try:
            manifest = _BundleManifestModel.model_validate(data)
        except ValidationError as exc:
            raise BundleManifestError(
                f"bundle manifest {path}: {_fmt_validation(exc)}"
            ) from exc
        # The manifest's ``subagent`` should match the file it lives in so a
        # copy-paste error can't silently load the wrong roster entry.
        if manifest.subagent != subagent:
            raise BundleManifestError(
                f"bundle manifest {path}: 'subagent' is '{manifest.subagent}' but "
                f"the file is named for '{subagent}'"
            )
        _reject_dupes(manifest.skills, path, "skills")
        _reject_dupes(manifest.hooks, path, "hooks")
        for name in (*manifest.skills, *manifest.hooks):
            if not _SLUG_RE.match(name):
                raise BundleManifestError(
                    f"bundle manifest {path}: artifact name '{name}' is invalid "
                    "(use lowercase letters, digits, '.', '_', '-')"
                )
        return manifest

    def _load_skill(self, name: str, manifest_path: Path) -> SkillArtifact:
        path = self._skill_path(name)
        if not path.is_file():
            raise BundleManifestError(
                f"bundle manifest {manifest_path}: skill '{name}' not found at {path}"
            )
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BundleManifestError(f"skill {path}: cannot read: {exc}") from exc
        return _parse_skill_md(text, fallback_name=name, source=path)

    def _load_hook(self, name: str, manifest_path: Path) -> HookArtifact:
        import json

        path = self._hook_path(name)
        if not path.is_file():
            raise BundleManifestError(
                f"bundle manifest {manifest_path}: hook '{name}' not found at {path}"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BundleManifestError(f"hook {path}: not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise BundleManifestError(f"hook {path}: must be a JSON object")
        data.setdefault("name", name)
        try:
            return HookArtifact.model_validate(data)
        except ValidationError as exc:
            raise BundleManifestError(f"hook {path}: {_fmt_validation(exc)}") from exc


def _reject_dupes(names: list[str], path: Path, section: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise BundleManifestError(
                f"bundle manifest {path}: '{section}' lists '{name}' more than once"
            )
        seen.add(name)


def _fmt_validation(exc: ValidationError) -> str:
    """Render a pydantic ValidationError into one readable line."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)
