"""Request-envelope schema for the backplane async-task path (#25).

Mirrors the canonical JSON-Schema in
``zelosbackplane/schemas/envelopes/v1/request.json``. That schema is
``additionalProperties: false`` with required top-level fields
``id, ts, source, kind, payload`` plus optional ``traceId`` and ``replyTo``.

Consequences for the wire shape (decisions baked in here):

* The **correlation id** the response envelope echoes back as ``corrId`` is
  the request envelope's ``id`` (per ``response.json``). We do not invent a
  separate ``corrId`` field on the request — that would violate
  ``additionalProperties: false``.
* The **reply topic** goes in the standard ``replyTo`` field. Per the issue's
  decision, replies use a per-request inbox (``_INBOX.<uuid>``).
* **Share coordinates** (``share_token`` / ``share_url`` / ``share_protocol`` /
  ``mount_hint``) and the kind-specific request body (``prompt`` / ``model`` /
  ``params``) live *inside* ``payload`` — the only object the schema lets us
  extend freely.

The model is validated against the bundled copy of the canonical schema via
:func:`validate_request_envelope` so a drift between this Pydantic model and
the JSON-Schema is caught in tests rather than at runtime against a live
backplane.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

# Topic prefix for inference requests. Subjects are ``inference.requests.<kind>``
# (e.g. ``inference.requests.codegen``) per zelosbackplane#11 + topics.yaml.
# Multi-tenant subject shaping is deferred — EA uses a single default tenant
# and keys the topic on the request kind alone.
REQUEST_TOPIC_PREFIX = "inference.requests"

# Component name stamped into the envelope ``source`` field.
SOURCE = "zelosmcp"

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas" / "v1"
_REQUEST_SCHEMA_PATH = _SCHEMA_DIR / "request.json"


def request_topic(kind: str) -> str:
    """Return the NATS subject for an inference request of ``kind``."""
    return f"{REQUEST_TOPIC_PREFIX}.{kind}"


def new_reply_inbox() -> str:
    """Return a fresh per-request reply inbox subject (``_INBOX.<uuid>``)."""
    return f"_INBOX.{uuid.uuid4().hex}"


def _utc_now_iso() -> str:
    """RFC 3339 / ISO-8601 UTC timestamp with a trailing ``Z``."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class ShareCoordinates(BaseModel):
    """Broker share coords carried in the envelope payload so a remote worker
    can mount the caller's workspace.

    Field names match the backplane contract
    (``share_token`` / ``share_url`` / ``share_protocol`` / ``mount_hint``).
    """

    model_config = ConfigDict(extra="forbid")

    share_token: str
    share_url: str
    share_protocol: str | None = None
    mount_hint: str | None = None


class RequestEnvelope(BaseModel):
    """A backplane v1 request envelope.

    Construct via :meth:`build` for the common case (auto-fills ``id`` / ``ts``
    / ``source`` and merges share coords into the payload). The model mirrors
    the canonical schema's top-level shape; ``additionalProperties`` is
    enforced as ``extra="forbid"`` so a stray top-level field fails fast,
    matching the JSON-Schema.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = Field(default_factory=_utc_now_iso)
    source: str = SOURCE
    kind: str
    payload: dict[str, Any]
    traceId: str | None = None
    replyTo: str | None = None

    @classmethod
    def build(
        cls,
        *,
        kind: str,
        payload: dict[str, Any],
        share: ShareCoordinates | None = None,
        reply_to: str | None = None,
        trace_id: str | None = None,
        envelope_id: str | None = None,
        ts: str | None = None,
    ) -> RequestEnvelope:
        """Construct a request envelope, folding share coords into ``payload``.

        The share coordinates are merged at the top level of ``payload`` (the
        schema only constrains the envelope's top level; ``payload`` is a free
        object), so a worker reads ``payload.share_token`` etc. alongside the
        kind-specific body (``payload.prompt`` / ``payload.model`` / ...).
        """
        body = dict(payload)
        if share is not None:
            body.update(share.model_dump(exclude_none=True))
        kwargs: dict[str, Any] = {
            "kind": kind,
            "payload": body,
            "replyTo": reply_to,
            "traceId": trace_id,
        }
        if envelope_id is not None:
            kwargs["id"] = envelope_id
        if ts is not None:
            kwargs["ts"] = ts
        return cls(**kwargs)

    def to_wire(self) -> dict[str, Any]:
        """Serialise to the wire dict, dropping unset optional fields.

        Optional fields (``traceId`` / ``replyTo``) are omitted when ``None``
        so the published JSON validates against the canonical schema, which
        does not allow them to be ``null``.
        """
        return self.model_dump(exclude_none=True)

    def to_json(self) -> bytes:
        """UTF-8 JSON bytes ready to publish on NATS."""
        return json.dumps(self.to_wire(), separators=(",", ":")).encode("utf-8")


@lru_cache(maxsize=1)
def _request_schema() -> dict[str, Any]:
    return json.loads(_REQUEST_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_request_envelope(envelope: RequestEnvelope | dict[str, Any]) -> None:
    """Validate an envelope's wire form against the canonical request schema.

    Raises ``jsonschema.ValidationError`` on any mismatch. Called by the
    publisher before every publish so a malformed envelope never reaches the
    backplane, and exercised directly by the unit tests.
    """
    wire = envelope.to_wire() if isinstance(envelope, RequestEnvelope) else envelope
    jsonschema.validate(instance=wire, schema=_request_schema())
