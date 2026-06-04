"""Backplane NATS publisher package (#25).

Async-task path: a tool constructs a request envelope, opens a broker share so
the remote worker can mount the caller's workspace, and publishes the envelope
onto ``inference.requests.<kind>`` over NATS. Replies come back on a per-request
reply topic, correlated by the envelope ``id``.

* :mod:`zelosmcp.backplane.envelope` — the request-envelope Pydantic model,
  matching ``zelosbackplane/schemas/envelopes/v1/request.json`` (validated
  against the bundled copy of that schema).
* :mod:`zelosmcp.backplane.publisher` — the ``nats-py``-backed publisher that
  reuses a connection across invocations.
"""

from __future__ import annotations

from zelosmcp.backplane.envelope import (
    RequestEnvelope,
    ShareCoordinates,
    request_topic,
    validate_request_envelope,
)
from zelosmcp.backplane.publisher import BackplaneError, BackplanePublisher

__all__ = [
    "RequestEnvelope",
    "ShareCoordinates",
    "BackplanePublisher",
    "BackplaneError",
    "request_topic",
    "validate_request_envelope",
]
