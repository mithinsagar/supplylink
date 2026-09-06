"""A stand-in for the GlobalParts vendor API.

This exists so the REST ingestion path is genuinely runnable and genuinely
testable, rather than being a code path that only ever executes against a mock.
It is a **test fixture that happens to speak HTTP**, not a product — it lives
under ``tools/`` and is never imported by ``supplylink``.

Two things make it worth having:

* the demo can be run end to end by anyone who clones the repository, with no
  vendor account and no network;
* the retry and backoff logic can be exercised against real HTTP responses,
  because the simulator will inject failures on request.

Failure injection is controlled by environment variables, so a compose file or a
test can turn it on without editing code:

``VENDOR_SIM_FAIL_FIRST_N``
    Fail the first N requests with 503 before serving normally. Demonstrates
    that retries recover a genuinely transient outage.
``VENDOR_SIM_FAIL_RATE``
    Fail this fraction of requests at random, for a flaky rather than a broken
    upstream.
``VENDOR_SIM_LATENCY_MS``
    Delay every response, for exercising timeouts.
``VENDOR_SIM_REQUIRE_TOKEN``
    Reject requests without the expected bearer token, so the 401-is-not-retried
    path can be demonstrated.

Run it with::

    uvicorn tools.vendor_sim.app:app --port 8081
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = Path(
    os.environ.get("VENDOR_SIM_CATALOG", REPO_ROOT / "data/samples/globalparts/catalog.json")
)

app = FastAPI(
    title="GlobalParts Vendor API (simulator)",
    version="1.0.0",
    description=(
        "A simulator used by SupplyLink's demo and integration tests. "
        "It is not part of the SupplyLink application."
    ),
)


class _State:
    """Request counters, so failure injection is deterministic across a run."""

    def __init__(self) -> None:
        self.requests = 0
        self.failures_served = 0

    def reset(self) -> None:
        self.requests = 0
        self.failures_served = 0


state = _State()


def _load_catalog() -> list[dict[str, Any]]:
    if not CATALOG_PATH.is_file():
        raise HTTPException(
            status_code=503,
            detail=(
                f"catalogue fixture not found at {CATALOG_PATH}. "
                f"Run: python scripts/generate_sample_data.py"
            ),
        )
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@app.middleware("http")
async def inject_failures(request: Request, call_next: Any) -> Any:
    """Simulate a flaky upstream, before any handler runs."""
    if request.url.path in {"/health", "/__sim/reset", "/__sim/stats"}:
        return await call_next(request)

    state.requests += 1

    latency_ms = _env_int("VENDOR_SIM_LATENCY_MS")
    if latency_ms:
        await asyncio.sleep(latency_ms / 1000)

    fail_first = _env_int("VENDOR_SIM_FAIL_FIRST_N")
    if fail_first and state.requests <= fail_first:
        state.failures_served += 1
        return JSONResponse(
            status_code=503,
            content={"error": "upstream temporarily unavailable", "request": state.requests},
            headers={"Retry-After": "1"},
        )

    fail_rate = _env_float("VENDOR_SIM_FAIL_RATE")
    if fail_rate and random.random() < fail_rate:  # noqa: S311 - fault injection, not crypto
        state.failures_served += 1
        return JSONResponse(status_code=502, content={"error": "bad gateway"})

    return await call_next(request)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "catalog": str(CATALOG_PATH), "requests_served": state.requests}


@app.get("/v1/catalog")
def catalog(
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=40, ge=1, le=500),  # noqa: N803 - the vendor's spelling
    warehouse: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """One page of the vendor catalogue."""
    expected = os.environ.get("VENDOR_SIM_REQUIRE_TOKEN")
    if expected and authorization != f"Bearer {expected}":
        # A 401 is not on any contract's retry list, so this demonstrates the
        # fail-fast path: no amount of retrying fixes a wrong credential.
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    records = _load_catalog()
    total = len(records)
    total_pages = max(1, -(-total // pageSize))
    start = (page - 1) * pageSize

    return {
        "items": records[start : start + pageSize],
        "meta": {
            "page": page,
            "pageSize": pageSize,
            "totalItems": total,
            "totalPages": total_pages,
            "warehouse": warehouse,
        },
    }


@app.post("/__sim/reset")
def reset() -> dict[str, Any]:
    """Reset the injection counters. Used between test cases."""
    state.reset()
    return {"status": "reset"}


@app.get("/__sim/stats")
def stats() -> dict[str, Any]:
    return {"requests": state.requests, "failures_served": state.failures_served}
