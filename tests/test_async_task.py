"""Tests for the backplane envelope + NATS publisher + async-task tool (#25).

The NATS substrate is faked through the publisher's injectable
``connect_factory`` (deterministic, no nats-server binary required — that
binary is not present in CI). Covers:

* Envelope construction + share-coord folding + schema validation against the
  bundled canonical request.json.
* Topic shaping (``inference.requests.<kind>``).
* Publisher publish → expected subject + payload + reply inbox; connection
  reuse; reconnect-on-token-change.
* The ``submit_inference_task`` tool: opens a share, publishes, returns
  ``{id, replyTopic}``, with the share coords present in the published payload.
"""

from __future__ import annotations

import json

import httpx
import jsonschema
import pytest
import respx

from zelosmcp.auth.identity import CallerIdentity
from zelosmcp.backplane.envelope import (
    RequestEnvelope,
    ShareCoordinates,
    new_reply_inbox,
    request_topic,
    validate_request_envelope,
)
from zelosmcp.backplane.publisher import BackplaneError, BackplanePublisher
from zelosmcp.broker.client import BrokerClient
from zelosmcp.tools.async_task import AsyncTaskDeps, submit_inference_task

KEY = "test-signing-key-which-is-32-bytes!!"
BASE = "http://broker.test"

SHARE_BODY = {
    "token": "share-xyz",
    "ttl_seconds": 600,
    "protocols": [
        {
            "kind": "webdav",
            "url": "http://broker.test/dav/share-xyz",
            "auth": {"scheme": "Bearer", "token": "p"},
        }
    ],
    "mount_hint": "/mnt/x",
    "expires_at": "2026-01-01T00:00:00Z",
}


# ── Fake NATS connection ───────────────────────────────────────────────────


class FakePublishedMsg:
    def __init__(self, subject, payload, reply):
        self.subject = subject
        self.payload = payload
        self.reply = reply


class FakeNATS:
    def __init__(self, url, token):
        self.url = url
        self.token = token
        self.published: list[FakePublishedMsg] = []
        self.flushed = 0
        self.drained = False
        self.is_closed = False

    async def publish(self, subject, payload, reply=None):
        self.published.append(FakePublishedMsg(subject, payload, reply))

    async def flush(self):
        self.flushed += 1

    async def request(self, subject, payload, timeout=None):
        self.published.append(FakePublishedMsg(subject, payload, "request"))

        class _Reply:
            data = b'{"ok": true}'

        return _Reply()

    async def drain(self):
        self.drained = True
        self.is_closed = True


def fake_connect_factory():
    conns: list[FakeNATS] = []

    async def _connect(url, token):
        nc = FakeNATS(url, token)
        conns.append(nc)
        return nc

    return _connect, conns


# ── Envelope ────────────────────────────────────────────────────────────


def test_request_topic():
    assert request_topic("codegen") == "inference.requests.codegen"
    assert request_topic("analysis") == "inference.requests.analysis"


def test_reply_inbox_shape():
    inbox = new_reply_inbox()
    assert inbox.startswith("_INBOX.")
    assert len(inbox) > len("_INBOX.")


def test_envelope_build_folds_share_into_payload():
    share = ShareCoordinates(
        share_token="tk", share_url="http://w/d", share_protocol="webdav", mount_hint="/m"
    )
    env = RequestEnvelope.build(
        kind="codegen",
        payload={"prompt": "hi", "model": "m1"},
        share=share,
        reply_to="_INBOX.abc",
        trace_id="user-1",
    )
    wire = env.to_wire()
    assert wire["kind"] == "codegen"
    assert wire["source"] == "zelosmcp"
    assert wire["replyTo"] == "_INBOX.abc"
    assert wire["traceId"] == "user-1"
    # Share coords live INSIDE payload (schema is additionalProperties:false).
    assert wire["payload"]["share_token"] == "tk"
    assert wire["payload"]["share_url"] == "http://w/d"
    assert wire["payload"]["share_protocol"] == "webdav"
    assert wire["payload"]["mount_hint"] == "/m"
    assert wire["payload"]["prompt"] == "hi"


def test_envelope_validates_against_canonical_schema():
    env = RequestEnvelope.build(kind="codegen", payload={"prompt": "hi"})
    validate_request_envelope(env)  # no raise
    # id is a uuid, ts an RFC3339 timestamp.
    wire = env.to_wire()
    assert "-" in wire["id"]
    assert wire["ts"].endswith("Z")


def test_envelope_omits_unset_optionals():
    env = RequestEnvelope.build(kind="codegen", payload={"prompt": "hi"})
    wire = env.to_wire()
    # No reply/trace set -> those keys absent (schema forbids null there).
    assert "traceId" not in wire
    # build() does not assign replyTo; the publisher does.
    assert "replyTo" not in wire
    validate_request_envelope(wire)


def test_envelope_rejects_extra_top_level_field():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RequestEnvelope(kind="codegen", payload={}, corrId="nope")  # type: ignore[call-arg]


def test_validate_rejects_missing_required():
    bad = {"id": "x", "ts": "t", "source": "s", "kind": "k"}  # no payload
    with pytest.raises(jsonschema.ValidationError):
        validate_request_envelope(bad)


# ── Publisher ─────────────────────────────────────────────────────────────


async def test_publish_request_returns_id_and_reply_topic():
    factory, conns = fake_connect_factory()
    pub = BackplanePublisher("nats://test:4222", connect_factory=factory)
    env = RequestEnvelope.build(kind="codegen", payload={"prompt": "hi"})
    result = await pub.publish_request(env, token="bp-token")
    assert result["id"] == env.id
    assert result["replyTopic"].startswith("_INBOX.")
    # One connection, one publish, flushed.
    assert len(conns) == 1
    assert conns[0].token == "bp-token"
    msg = conns[0].published[0]
    assert msg.subject == "inference.requests.codegen"
    assert msg.reply == result["replyTopic"]
    payload = json.loads(msg.payload)
    assert payload["payload"]["prompt"] == "hi"
    assert conns[0].flushed == 1


async def test_publish_reuses_connection_same_token():
    factory, conns = fake_connect_factory()
    pub = BackplanePublisher("nats://test:4222", connect_factory=factory)
    await pub.publish_request(RequestEnvelope.build(kind="codegen", payload={"p": 1}), token="t")
    await pub.publish_request(RequestEnvelope.build(kind="codegen", payload={"p": 2}), token="t")
    assert len(conns) == 1  # connection reused


async def test_publish_reconnects_on_token_change():
    factory, conns = fake_connect_factory()
    pub = BackplanePublisher("nats://test:4222", connect_factory=factory)
    await pub.publish_request(RequestEnvelope.build(kind="codegen", payload={"p": 1}), token="t1")
    await pub.publish_request(RequestEnvelope.build(kind="codegen", payload={"p": 2}), token="t2")
    assert len(conns) == 2
    assert conns[0].drained is True  # old connection drained


async def test_request_reply_round_trip():
    factory, conns = fake_connect_factory()
    pub = BackplanePublisher("nats://test:4222", connect_factory=factory)
    env = RequestEnvelope.build(kind="analysis", payload={"prompt": "x"})
    data = await pub.request_reply(env, token="t")
    assert json.loads(data) == {"ok": True}
    assert conns[0].published[0].subject == "inference.requests.analysis"


async def test_publish_connect_failure_raises_backplane_error():
    async def bad_connect(url, token):
        raise OSError("no route")

    pub = BackplanePublisher("nats://test:4222", connect_factory=bad_connect)
    with pytest.raises(BackplaneError):
        await pub.publish_request(RequestEnvelope.build(kind="codegen", payload={}))


def test_publisher_requires_url(monkeypatch):
    monkeypatch.delenv("ZELOSBACKPLANE_URL", raising=False)
    with pytest.raises(ValueError):
        BackplanePublisher()


# ── submit_inference_task tool ─────────────────────────────────────────────


@respx.mock
async def test_submit_inference_task_opens_share_and_publishes():
    respx.post(f"{BASE}/shares").mock(return_value=httpx.Response(201, json=SHARE_BODY))
    factory, conns = fake_connect_factory()
    ident = CallerIdentity(subject="user-77", scopes=("infer",))
    async with BrokerClient(BASE) as broker:
        pub = BackplanePublisher("nats://test:4222", connect_factory=factory)
        deps = AsyncTaskDeps(
            broker=broker, publisher=pub, identity=ident, signing_key=KEY
        )
        result = await submit_inference_task(
            prompt="generate a function",
            deps=deps,
            model="qwen",
            params={"temperature": 0.2},
            kind="codegen",
            workspace_path="/repo",
        )
    assert set(result.keys()) == {"id", "replyTopic"}
    assert result["replyTopic"].startswith("_INBOX.")
    # Published envelope carries share coords + body.
    payload = json.loads(conns[0].published[0].payload)
    assert conns[0].published[0].subject == "inference.requests.codegen"
    body = payload["payload"]
    assert body["prompt"] == "generate a function"
    assert body["model"] == "qwen"
    assert body["params"] == {"temperature": 0.2}
    assert body["share_token"] == "share-xyz"
    assert body["share_url"] == "http://broker.test/dav/share-xyz"
    assert body["share_protocol"] == "webdav"
    assert body["mount_hint"] == "/mnt/x"
    assert payload["traceId"] == "user-77"
    # Backplane connected with a backplane-audience token.
    from zelosmcp.auth.token import AUDIENCE_BACKPLANE, verify_token

    claims = verify_token(conns[0].token, AUDIENCE_BACKPLANE, signing_key=KEY)
    assert claims["sub"] == "user-77"


@respx.mock
async def test_submit_inference_task_without_share():
    factory, conns = fake_connect_factory()
    ident = CallerIdentity(subject="user-1")
    async with BrokerClient(BASE) as broker:
        pub = BackplanePublisher("nats://test:4222", connect_factory=factory)
        deps = AsyncTaskDeps(
            broker=broker, publisher=pub, identity=ident, signing_key=KEY, open_share=False
        )
        result = await submit_inference_task(prompt="hi", deps=deps)
    assert "id" in result
    payload = json.loads(conns[0].published[0].payload)
    assert "share_token" not in payload["payload"]
    # Validates against the canonical schema.
    validate_request_envelope(payload)
