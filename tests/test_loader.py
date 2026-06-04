"""Tests for the subagent artifact loader (#27).

Covers:

* the filesystem :class:`ArtifactStore` reading skills + hooks + bundle
  manifests from a fixture artifact dir;
* manifest validation — malformed / dangling / typo'd manifests fail fast with
  a readable :class:`BundleManifestError`, not a runtime exception mid-turn;
* :func:`apply_bundle` assembling env / system-prompt fragments / hook tools
  and the compact ``open_payload`` reference;
* the invoke-time integration: ``run_sync_subagent`` loads + applies a bundle,
  carries the reference in the broker ``open`` channel body, and rides the
  skill fragments along as first-turn context.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from zelosmcp.auth.identity import CallerIdentity
from zelosmcp.broker.client import BrokerClient
from zelosmcp.broker.schema import OpenFrame, TurnEndFrame, encode_frame
from zelosmcp.loader import (
    ArtifactStore,
    BundleManifestError,
    apply_bundle,
    get_subagent,
    load_subagent_bundle,
)
from zelosmcp.loader.store import resolve_artifacts_dir
from zelosmcp.tools.sync_subagent import SubagentDeps, run_sync_subagent

KEY = "test-signing-key-which-is-32-bytes!!"
BASE = "http://broker.test"
CHANNEL_BODY = {"session_id": "sess-9", "attach_url": "/sync/channels/sess-9/attach"}


# ── Fixture artifact dir ─────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def artifacts_dir(tmp_path: Path) -> Path:
    """Build a sample artifact tree with one skill, one hook, one bundle."""
    root = tmp_path / "artifacts"
    _write(
        root / "skills" / "review-checklist" / "SKILL.md",
        (
            "---\n"
            "name: review-checklist\n"
            "description: A checklist for reviewing Python diffs.\n"
            "paths:\n"
            "  - '**/*.py'\n"
            "---\n"
            "# Review checklist\n\nCheck for off-by-one errors.\n"
        ),
    )
    _write(
        root / "hooks" / "lint-on-edit.json",
        json.dumps({"event": "afterFileEdit", "command": "ruff check ."}),
    )
    _write(
        root / "bundles" / "Plan.yaml",
        (
            "subagent: Plan\n"
            "skills:\n"
            "  - review-checklist\n"
            "hooks:\n"
            "  - lint-on-edit\n"
            "env:\n"
            "  ZELOS_REVIEW_MODE: strict\n"
        ),
    )
    return root


# ── resolve_artifacts_dir ─────────────────────────────────────────────────


def test_resolve_artifacts_dir_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZELOS_ARTIFACTS_DIR", str(tmp_path / "custom"))
    assert resolve_artifacts_dir() == tmp_path / "custom"


def test_resolve_artifacts_dir_default_under_state_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("ZELOS_ARTIFACTS_DIR", raising=False)
    monkeypatch.setenv("ZELOSMCP_STATE_DIR", str(tmp_path / "state"))
    assert resolve_artifacts_dir() == tmp_path / "state" / "artifacts"


# ── ArtifactStore.load_bundle ──────────────────────────────────────────────


def test_load_bundle_resolves_skills_hooks_env(artifacts_dir):
    store = ArtifactStore(artifacts_dir)
    bundle = store.load_bundle("Plan")
    assert bundle is not None
    assert bundle.subagent == "Plan"
    assert [s.name for s in bundle.skills] == ["review-checklist"]
    skill = bundle.skills[0]
    assert skill.description == "A checklist for reviewing Python diffs."
    assert skill.paths == ["**/*.py"]
    assert "off-by-one" in skill.body
    assert [h.name for h in bundle.hooks] == ["lint-on-edit"]
    assert bundle.hooks[0].event == "afterFileEdit"
    assert bundle.hooks[0].command == "ruff check ."
    assert bundle.env == {"ZELOS_REVIEW_MODE": "strict"}


def test_load_bundle_missing_returns_none(artifacts_dir):
    # A subagent with no manifest launches bare (None, not an error).
    assert ArtifactStore(artifacts_dir).load_bundle("Explore") is None


def test_has_bundle(artifacts_dir):
    store = ArtifactStore(artifacts_dir)
    assert store.has_bundle("Plan") is True
    assert store.has_bundle("Explore") is False


def test_skill_without_frontmatter_uses_dir_name_and_body(tmp_path):
    root = tmp_path / "artifacts"
    _write(root / "skills" / "bare" / "SKILL.md", "Just a body, no frontmatter.\n")
    _write(root / "bundles" / "Plan.yaml", "subagent: Plan\nskills:\n  - bare\n")
    bundle = ArtifactStore(root).load_bundle("Plan")
    assert bundle is not None
    assert bundle.skills[0].name == "bare"
    assert bundle.skills[0].description == ""
    assert "Just a body" in bundle.skills[0].body


# ── Manifest validation (fail-fast) ────────────────────────────────────────


def test_malformed_yaml_manifest_raises_readable(tmp_path):
    root = tmp_path / "artifacts"
    _write(root / "bundles" / "Plan.yaml", "subagent: Plan\nskills: [unterminated\n")
    with pytest.raises(BundleManifestError) as exc:
        ArtifactStore(root).load_bundle("Plan")
    assert "not valid YAML" in str(exc.value)


def test_unknown_field_in_manifest_rejected(tmp_path):
    root = tmp_path / "artifacts"
    _write(root / "bundles" / "Plan.yaml", "subagent: Plan\nskils:\n  - x\n")
    with pytest.raises(BundleManifestError) as exc:
        ArtifactStore(root).load_bundle("Plan")
    assert "skils" in str(exc.value)


def test_manifest_subagent_mismatch_rejected(tmp_path):
    root = tmp_path / "artifacts"
    _write(root / "bundles" / "Plan.yaml", "subagent: Explore\n")
    with pytest.raises(BundleManifestError) as exc:
        ArtifactStore(root).load_bundle("Plan")
    assert "Explore" in str(exc.value)


def test_dangling_skill_reference_raises(tmp_path):
    root = tmp_path / "artifacts"
    _write(root / "bundles" / "Plan.yaml", "subagent: Plan\nskills:\n  - ghost\n")
    with pytest.raises(BundleManifestError) as exc:
        ArtifactStore(root).load_bundle("Plan")
    assert "ghost" in str(exc.value) and "not found" in str(exc.value)


def test_dangling_hook_reference_raises(tmp_path):
    root = tmp_path / "artifacts"
    _write(root / "bundles" / "Plan.yaml", "subagent: Plan\nhooks:\n  - ghost\n")
    with pytest.raises(BundleManifestError) as exc:
        ArtifactStore(root).load_bundle("Plan")
    assert "ghost" in str(exc.value) and "not found" in str(exc.value)


def test_duplicate_artifact_in_manifest_rejected(tmp_path):
    root = tmp_path / "artifacts"
    _write(root / "skills" / "a" / "SKILL.md", "body")
    _write(root / "bundles" / "Plan.yaml", "subagent: Plan\nskills:\n  - a\n  - a\n")
    with pytest.raises(BundleManifestError) as exc:
        ArtifactStore(root).load_bundle("Plan")
    assert "more than once" in str(exc.value)


def test_invalid_artifact_name_rejected(tmp_path):
    root = tmp_path / "artifacts"
    _write(root / "bundles" / "Plan.yaml", "subagent: Plan\nskills:\n  - '../escape'\n")
    with pytest.raises(BundleManifestError) as exc:
        ArtifactStore(root).load_bundle("Plan")
    assert "invalid" in str(exc.value)


def test_malformed_hook_json_raises(tmp_path):
    root = tmp_path / "artifacts"
    _write(root / "hooks" / "bad.json", "{ not json")
    _write(root / "bundles" / "Plan.yaml", "subagent: Plan\nhooks:\n  - bad\n")
    with pytest.raises(BundleManifestError) as exc:
        ArtifactStore(root).load_bundle("Plan")
    assert "not valid JSON" in str(exc.value)


def test_hook_missing_command_raises(tmp_path):
    root = tmp_path / "artifacts"
    _write(root / "hooks" / "noc.json", json.dumps({"event": "afterFileEdit"}))
    _write(root / "bundles" / "Plan.yaml", "subagent: Plan\nhooks:\n  - noc\n")
    with pytest.raises(BundleManifestError):
        ArtifactStore(root).load_bundle("Plan")


# ── apply_bundle ────────────────────────────────────────────────────────────


def test_apply_bundle_assembles_fragments_hooks_env(artifacts_dir):
    store = ArtifactStore(artifacts_dir)
    applied = apply_bundle(store.load_bundle("Plan"))
    assert applied is not None
    assert applied.env == {"ZELOS_REVIEW_MODE": "strict"}
    # One system-prompt fragment per skill, leading with name + description.
    assert len(applied.system_prompt_fragments) == 1
    frag = applied.system_prompt_fragments[0]
    assert "# Skill: review-checklist" in frag
    assert "A checklist for reviewing Python diffs." in frag
    assert "off-by-one" in frag
    # Hook tool reference carries event + command.
    assert applied.hook_tools == [
        {"name": "lint-on-edit", "event": "afterFileEdit", "command": "ruff check ."}
    ]


def test_apply_bundle_open_payload_is_compact_and_value_free(artifacts_dir):
    store = ArtifactStore(artifacts_dir)
    applied = apply_bundle(store.load_bundle("Plan"))
    assert applied is not None
    payload = applied.open_payload()
    # Skill refs carry name + description (the issue's verification anchor),
    # NOT the full body.
    assert payload["skills"] == [
        {
            "name": "review-checklist",
            "description": "A checklist for reviewing Python diffs.",
            "paths": ["**/*.py"],
        }
    ]
    assert payload["hooks"][0]["command"] == "ruff check ."
    # Only env KEYS travel in the open frame — values may be secret.
    assert payload["env"] == ["ZELOS_REVIEW_MODE"]
    assert "strict" not in json.dumps(payload)


def test_apply_bundle_none_and_empty(tmp_path):
    assert apply_bundle(None) is None
    root = tmp_path / "artifacts"
    _write(root / "bundles" / "Plan.yaml", "subagent: Plan\n")  # no skills/hooks/env
    assert apply_bundle(ArtifactStore(root).load_bundle("Plan")) is None


def test_load_subagent_bundle_convenience(artifacts_dir):
    applied = load_subagent_bundle("Plan", store=ArtifactStore(artifacts_dir))
    assert applied is not None and applied.subagent == "Plan"
    assert load_subagent_bundle("Explore", store=ArtifactStore(artifacts_dir)) is None


# ── Invoke-time integration with run_sync_subagent ──────────────────────────


def _channel_factory(frames):
    box = {}

    def _factory(attach_url, auth_header):
        from zelosmcp.broker.sync_channel import SyncChannelClient

        class FakeWS:
            def __init__(self):
                self._incoming = list(frames)
                self.sent = []
                self.closed = False

            async def send(self, data):
                self.sent.append(data)

            async def recv(self):
                if not self._incoming:
                    from websockets.exceptions import ConnectionClosedOK

                    raise ConnectionClosedOK(None, None)
                return self._incoming.pop(0)

            async def close(self):
                self.closed = True

        async def _connect(url, headers):
            ws = FakeWS()
            box["ws"] = ws
            return ws

        return SyncChannelClient(attach_url, auth_header=auth_header, connect_factory=_connect)

    return _factory, box


@respx.mock
async def test_run_sync_subagent_delivers_bundle(artifacts_dir):
    open_chan = respx.post(f"{BASE}/sync/channels").mock(
        return_value=httpx.Response(201, json=CHANNEL_BODY)
    )
    respx.post(f"{BASE}/sync/channels/sess-9/close").mock(return_value=httpx.Response(204))

    frames = [
        encode_frame(OpenFrame(session_id="sess-9", subagent="Plan")),
        encode_frame(TurnEndFrame(session_id="sess-9", turn_id="t1", message={"text": "ok"})),
    ]
    factory, box = _channel_factory(frames)
    loader = lambda name: load_subagent_bundle(name, store=ArtifactStore(artifacts_dir))  # noqa: E731

    async with BrokerClient(BASE) as broker:
        deps = SubagentDeps(
            broker=broker,
            channel_factory=factory,
            identity=CallerIdentity(subject="dev-1"),
            signing_key=KEY,
            bundle_loader=loader,
        )
        result = await run_sync_subagent(
            get_subagent("plan"), prompt="plan it", deps=deps, open_share=False
        )

    # The open-channel request carried the compact bundle reference.
    body = json.loads(open_chan.calls.last.request.content)
    assert body["subagent"] == "Plan"
    assert body["bundle"]["skills"][0]["name"] == "review-checklist"
    assert body["bundle"]["env"] == ["ZELOS_REVIEW_MODE"]

    # The first turn frame carried the skill fragment as context.
    sent = json.loads(box["ws"].sent[0])
    assert sent["kind"] == "turn"
    assert "review-checklist" in sent["content"]["context"]
    assert "off-by-one" in sent["content"]["context"]

    # The result echoes the bundle reference.
    assert result.bundle["skills"][0]["description"] == "A checklist for reviewing Python diffs."


@respx.mock
async def test_run_sync_subagent_bare_when_no_bundle(artifacts_dir):
    open_chan = respx.post(f"{BASE}/sync/channels").mock(
        return_value=httpx.Response(201, json=CHANNEL_BODY)
    )
    respx.post(f"{BASE}/sync/channels/sess-9/close").mock(return_value=httpx.Response(204))

    frames = [encode_frame(TurnEndFrame(session_id="sess-9", turn_id="t1", message={"text": "ok"}))]
    factory, box = _channel_factory(frames)
    loader = lambda name: load_subagent_bundle(name, store=ArtifactStore(artifacts_dir))  # noqa: E731

    async with BrokerClient(BASE) as broker:
        deps = SubagentDeps(
            broker=broker,
            channel_factory=factory,
            identity=CallerIdentity(subject="dev-1"),
            signing_key=KEY,
            bundle_loader=loader,
        )
        # "explore" has no manifest in the fixture -> bare launch.
        result = await run_sync_subagent(
            get_subagent("explore"), prompt="look", deps=deps, open_share=False
        )

    body = json.loads(open_chan.calls.last.request.content)
    assert "bundle" not in body
    assert result.bundle is None
    sent = json.loads(box["ws"].sent[0])
    assert "context" not in sent["content"]


@respx.mock
async def test_run_sync_subagent_fails_fast_on_bad_manifest(tmp_path):
    # A malformed manifest must raise BEFORE any broker resource is allocated.
    root = tmp_path / "artifacts"
    _write(root / "bundles" / "Plan.yaml", "subagent: Plan\nskills:\n  - ghost\n")
    create = respx.post(f"{BASE}/sync/channels").mock(return_value=httpx.Response(201, json=CHANNEL_BODY))
    factory, _ = _channel_factory([])
    loader = lambda name: load_subagent_bundle(name, store=ArtifactStore(root))  # noqa: E731

    async with BrokerClient(BASE) as broker:
        deps = SubagentDeps(
            broker=broker,
            channel_factory=factory,
            identity=CallerIdentity(subject="dev-1"),
            signing_key=KEY,
            bundle_loader=loader,
        )
        with pytest.raises(BundleManifestError):
            await run_sync_subagent(
                get_subagent("plan"), prompt="x", deps=deps, open_share=False
            )
    # No channel was opened — the failure was pre-flight.
    assert not create.called
