"""Health-probe HTTP routes honoring the Zelos suite container contract.

Standard endpoints (zelosai/docs/architecture/07-container-contract.md):

* ``GET /healthz`` — liveness (process is alive).
* ``GET /readyz``  — readiness (dependencies reachable, PVC writable, ...).

These are aliases over the richer ``/api/status`` endpoint so the operator's
standard probe configuration (httpGet :http /healthz, /readyz) works without
component-specific branching.

Note: ``GET /`` is deliberately *not* a health alias — that path serves the
web dashboard UI (see ``routes/pages.py``). The k8s probes in
``deploy/kubernetes/zelosmcp.yaml`` target ``/healthz`` and ``/readyz``, so
the contract's sanity-probe intent is fully covered without shadowing the UI.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:  # pragma: no cover
    from zelosmcp.manager import ProxyManager


def make_routes(manager: "ProxyManager") -> list[Route]:
    async def healthz(_req: Request) -> JSONResponse:
        payload: dict[str, object] = {"status": "ok"}
        # Surface broker reachability when ZELOS_BROKER_URL is configured
        # (issue #23 verification). Best-effort: a probe failure marks the
        # broker unreachable but never fails liveness — zelosmcp is alive
        # even if the broker is down. Skipped entirely when unconfigured so
        # deployments without the data path see no extra latency.
        broker = await _probe_broker()
        if broker is not None:
            payload["broker"] = broker
        return JSONResponse(payload)

    async def readyz(_req: Request) -> JSONResponse:
        # Ready as soon as the ProxyManager is initialized; passthrough pool
        # warmup is async and best-effort.
        return JSONResponse({"status": "ready"})

    return [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
    ]


async def _probe_broker() -> dict[str, object] | None:
    """Probe broker reachability when ``ZELOS_BROKER_URL`` is set.

    Returns ``{"configured": bool, "reachable": bool, "url": str}`` or
    ``None`` when the broker is not configured (so ``/healthz`` stays a bare
    ``{"status": "ok"}`` for deployments without the data path).
    """
    from zelosmcp.broker.client import BROKER_URL_ENV, BrokerClient

    url = os.environ.get(BROKER_URL_ENV)
    if not url:
        return None
    try:
        async with BrokerClient(url) as broker:
            reachable = await broker.healthy()
    except Exception:
        reachable = False
    return {"configured": True, "reachable": reachable, "url": url}
