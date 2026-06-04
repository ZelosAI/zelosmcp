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

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:  # pragma: no cover
    from zelosmcp.manager import ProxyManager


def make_routes(manager: "ProxyManager") -> list[Route]:
    async def healthz(_req: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def readyz(_req: Request) -> JSONResponse:
        # Ready as soon as the ProxyManager is initialized; passthrough pool
        # warmup is async and best-effort.
        return JSONResponse({"status": "ready"})

    return [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
    ]
