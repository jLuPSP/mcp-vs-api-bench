"""Synthetic enterprise ops API. The control term of the whole benchmark.

Every arm reaches this exact process. The MCP servers are thin wrappers over these same
endpoints, so what the benchmark measures is the access layer, not two implementations.

Two properties matter for benchmark validity:

1. `BACKEND_LATENCY_MS` injects a deterministic delay (jitter 0 by default) so upstream
   work is a constant that can be subtracted out to isolate protocol overhead.
2. Every response carries `X-Backend-Elapsed-Ms`, the server's own view of how long it
   spent. The harness subtracts this from wall-clock to get protocol overhead without
   having to assume the injected latency was exact.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .data import ONCALL, World

LATENCY_MS = float(os.getenv("BACKEND_LATENCY_MS", "40"))
JITTER_MS = float(os.getenv("BACKEND_JITTER_MS", "0"))

# Enterprise mode pads every record with the fields a real system of record actually
# returns: SLA state, custom fields, provenance, integration ids, nested summaries.
#
# This is not padding for its own sake. It changes the token economics in a direction that
# works AGAINST this benchmark's own headline finding. When tool results are compact, the
# schema block dominates context and the MCP-vs-direct schema delta looks decisive. When
# results are realistically fat, results dominate and the schema delta shrinks as a share
# of the bill. Anyone quoting the schema numbers should know which regime they measured.
ENTERPRISE = os.getenv("BENCH_ENTERPRISE", "0") == "1"


def _enrich_ticket(t: dict[str, Any]) -> dict[str, Any]:
    if not ENTERPRISE:
        return t
    tid = t["id"]
    n = int(tid.split("-")[-1])
    return {
        **t,
        "sla": {
            "target_response_mins": [15, 60, 240, 480][n % 4],
            "target_resolution_hours": [4, 24, 72, 168][n % 4],
            "breached": n % 7 == 0,
            "paused": n % 11 == 0,
        },
        "channel": ["email", "portal", "phone", "chat", "api"][n % 5],
        "reporter_email": f"user{n}@example-corp.invalid",
        "watchers": [f"watcher{(n + i) % 9}@example-corp.invalid" for i in range(n % 3)],
        "custom_fields": {
            "cost_centre": f"CC-{4000 + (n % 40)}",
            "environment": ["prod", "staging", "dev"][n % 3],
            "regulatory_scope": ["none", "sox", "gdpr", "pci"][n % 4],
            "reopened_count": n % 3,
        },
        "linked_records": {
            "change_request": f"CHG-{9000 + n}" if n % 4 == 0 else None,
            "known_error": f"KE-{700 + (n % 25)}" if n % 6 == 0 else None,
        },
        "last_updated_at": t["opened_at"],
        "source_system": "servicedesk-prod",
        "record_version": 1 + (n % 5),
    }


def _enrich_customer(c: dict[str, Any]) -> dict[str, Any]:
    if not ENTERPRISE:
        return c
    n = int(c["id"].split("-")[-1])
    return {
        **c,
        "contract": {
            "renewal_date": "2026-11-30",
            "arr_band": ["<50k", "50-250k", "250k-1m", ">1m"][n % 4],
            "auto_renew": n % 2 == 0,
            "support_plan": ["standard", "premium", "signature"][n % 3],
        },
        "billing_account_id": f"BA-{60000 + n}",
        "crm_record_id": f"006{n:07d}",
        "primary_contact": {
            "name": f"Contact {n}",
            "email": f"contact{n}@example-corp.invalid",
            "timezone": ["Europe/London", "America/New_York", "Asia/Singapore"][n % 3],
        },
        "health_score": 40 + (n % 60),
        "source_system": "crm-prod",
    }


def _enrich_item(i: dict[str, Any]) -> dict[str, Any]:
    if not ENTERPRISE:
        return i
    n = int(i["sku"].split("-")[-1])
    return {
        **i,
        "unit_cost": round(4.5 + (n % 37) * 1.15, 2),
        "currency": "USD",
        "lead_time_days": 3 + (n % 21),
        "supplier": {
            "id": f"SUP-{200 + (n % 12)}",
            "name": f"Supplier {200 + (n % 12)}",
            "preferred": n % 3 == 0,
        },
        "bin_location": f"{chr(65 + n % 6)}-{10 + n % 40}-{n % 8}",
        "cycle_count_due": n % 5 == 0,
        "hazmat": n % 17 == 0,
        "source_system": "wms-prod",
    }

app = FastAPI(title="ops-api", version="1.0.0")
world = World()
_jitter_rng = random.Random(20260301)


async def _work() -> None:
    """Simulate upstream service time. Deterministic unless jitter is enabled."""
    delay = LATENCY_MS
    if JITTER_MS:
        delay += _jitter_rng.uniform(-JITTER_MS, JITTER_MS)
    if delay > 0:
        await asyncio.sleep(delay / 1000.0)


@app.middleware("http")
async def timing(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    response.headers["X-Backend-Elapsed-Ms"] = f"{elapsed_ms:.4f}"
    return response


# --- health and benchmark control -------------------------------------------


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "latency_ms": LATENCY_MS,
        "jitter_ms": JITTER_MS,
        "enterprise_records": ENTERPRISE,
    }


@app.post("/_bench/reset")
async def bench_reset() -> dict[str, Any]:
    """Rebuild world state from the seed. Called before every trial."""
    world.reset()
    return {"ok": True, "tickets": len(world.tickets), "customers": len(world.customers)}


@app.get("/_bench/state")
async def bench_state() -> dict[str, Any]:
    """Full state snapshot. Used by task checkers, never exposed as a tool."""
    return {
        "tickets": {k: vars(v) for k, v in world.tickets.items()},
        "customers": {k: vars(v) for k, v in world.customers.items()},
        "inventory": {k: vars(v) for k, v in world.inventory.items()},
        "audit": [vars(a) for a in world.audit],
        "oncall": ONCALL,
    }


# --- tickets -----------------------------------------------------------------


@app.get("/tickets")
async def list_tickets(
    status: str | None = None,
    priority: str | None = None,
    assignee: str | None = None,
    customer_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    await _work()
    rows = world.list_tickets(status, priority, assignee, customer_id, limit)
    return {"count": len(rows), "tickets": [_enrich_ticket(r) for r in rows]}


@app.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: str) -> dict[str, Any]:
    await _work()
    if ticket_id not in world.tickets:
        raise HTTPException(status_code=404, detail=f"no such ticket: {ticket_id}")
    return _enrich_ticket(vars(world.tickets[ticket_id]))


class AssignBody(BaseModel):
    assignee: str = Field(..., description="Engineer username, e.g. r.okonkwo")


@app.post("/tickets/{ticket_id}/assign")
async def assign_ticket(ticket_id: str, body: AssignBody) -> dict[str, Any]:
    await _work()
    if ticket_id not in world.tickets:
        raise HTTPException(status_code=404, detail=f"no such ticket: {ticket_id}")
    return world.assign_ticket(ticket_id, body.assignee)


class PriorityBody(BaseModel):
    priority: str = Field(..., description="One of P1, P2, P3, P4")


@app.post("/tickets/{ticket_id}/priority")
async def set_priority(ticket_id: str, body: PriorityBody) -> dict[str, Any]:
    await _work()
    if ticket_id not in world.tickets:
        raise HTTPException(status_code=404, detail=f"no such ticket: {ticket_id}")
    try:
        return world.set_ticket_priority(ticket_id, body.priority)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class StatusBody(BaseModel):
    status: str = Field(..., description="One of open, in_progress, blocked, resolved")


@app.post("/tickets/{ticket_id}/status")
async def set_status(ticket_id: str, body: StatusBody) -> dict[str, Any]:
    await _work()
    if ticket_id not in world.tickets:
        raise HTTPException(status_code=404, detail=f"no such ticket: {ticket_id}")
    try:
        return world.set_ticket_status(ticket_id, body.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- customers ---------------------------------------------------------------


@app.get("/customers")
async def list_customers(q: str | None = None, tier: str | None = None) -> dict[str, Any]:
    await _work()
    rows = world.list_customers(q, tier)
    return {"count": len(rows), "customers": [_enrich_customer(r) for r in rows]}


@app.get("/customers/{customer_id}")
async def get_customer(customer_id: str) -> dict[str, Any]:
    await _work()
    if customer_id not in world.customers:
        raise HTTPException(status_code=404, detail=f"no such customer: {customer_id}")
    return _enrich_customer(vars(world.customers[customer_id]))


# --- inventory ---------------------------------------------------------------


@app.get("/inventory")
async def list_inventory(below_reorder: bool = False, warehouse: str | None = None) -> dict[str, Any]:
    await _work()
    rows = world.list_inventory(below_reorder, warehouse)
    return {"count": len(rows), "items": [_enrich_item(r) for r in rows]}


class AdjustBody(BaseModel):
    delta: int = Field(..., description="Signed change to on-hand quantity")


@app.post("/inventory/{sku}/adjust")
async def adjust_inventory(sku: str, body: AdjustBody) -> dict[str, Any]:
    await _work()
    if sku not in world.inventory:
        raise HTTPException(status_code=404, detail=f"no such sku: {sku}")
    return world.adjust_inventory(sku, body.delta)


# --- audit -------------------------------------------------------------------


@app.get("/audit")
async def list_audit(limit: int = 20) -> dict[str, Any]:
    await _work()
    rows = [vars(a) for a in world.audit[-limit:]]
    return {"count": len(rows), "entries": rows}


# --- oncall ------------------------------------------------------------------


@app.get("/oncall")
async def get_oncall() -> dict[str, Any]:
    await _work()
    return {"oncall": ONCALL}


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("BACKEND_HOST", "0.0.0.0"),
        port=int(os.getenv("BACKEND_PORT", "9110")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
