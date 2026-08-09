"""Deterministic seed data for the synthetic enterprise ops backend.

Everything here is generated from a fixed seed so that every arm, every trial, and every
machine sees byte-identical state. `reset()` rebuilds the world; the harness calls it
before each trial so a mutating task cannot leak into the next one.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any

SEED = int(os.getenv("BENCH_SEED", "1729"))

# Fixed epoch so relative dates never drift with wall-clock time.
EPOCH = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)

PRIORITIES = ["P1", "P2", "P3", "P4"]
STATUSES = ["open", "in_progress", "blocked", "resolved"]
ENGINEERS = ["r.okonkwo", "s.lindqvist", "m.tanaka", "a.vasquez", "j.byrne"]
ONCALL = "r.okonkwo"

REGIONS = ["emea", "amer", "apac"]
TIERS = ["enterprise", "business", "starter"]

SUBJECTS = [
    "Checkout latency spike in {region}",
    "SSO callback returns 500 for {tier} tenants",
    "Nightly export job stalled",
    "Duplicate invoices on plan change",
    "Webhook retries exhausted",
    "Search index lagging behind writes",
    "Rate limit applied to allowlisted IP",
    "PDF render times out on large orders",
    "Bulk import drops trailing rows",
    "Mobile session expires early",
]


@dataclass
class Customer:
    id: str
    name: str
    tier: str
    region: str
    account_owner: str
    seats: int


@dataclass
class Ticket:
    id: str
    subject: str
    customer_id: str
    priority: str
    status: str
    assignee: str | None
    opened_at: str
    tags: list[str] = field(default_factory=list)


@dataclass
class InventoryItem:
    sku: str
    name: str
    on_hand: int
    reorder_point: int
    target_level: int
    warehouse: str


@dataclass
class AuditEntry:
    seq: int
    at: str
    actor: str
    action: str
    target: str
    detail: str


class World:
    """Mutable benchmark state. One instance per backend process."""

    def __init__(self) -> None:
        self.customers: dict[str, Customer] = {}
        self.tickets: dict[str, Ticket] = {}
        self.inventory: dict[str, InventoryItem] = {}
        self.audit: list[AuditEntry] = []
        self._audit_seq = 0
        self.reset()

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        """Rebuild the world from the fixed seed. Idempotent."""
        rng = random.Random(SEED)
        self.customers = {}
        self.tickets = {}
        self.inventory = {}
        self.audit = []
        self._audit_seq = 0

        for i in range(24):
            cid = f"CUST-{1000 + i}"
            self.customers[cid] = Customer(
                id=cid,
                name=_company_name(rng, i),
                tier=rng.choice(TIERS),
                region=rng.choice(REGIONS),
                account_owner=rng.choice(ENGINEERS),
                seats=rng.choice([12, 40, 85, 150, 400, 1200]),
            )

        customer_ids = list(self.customers)
        for i in range(60):
            tid = f"TKT-{2000 + i}"
            cust = rng.choice(customer_ids)
            # Weight toward lower priorities so P1 stays a small, checkable set.
            priority = rng.choices(PRIORITIES, weights=[1, 3, 5, 4])[0]
            status = rng.choices(STATUSES, weights=[5, 3, 1, 4])[0]
            assignee = None if status == "open" and rng.random() < 0.7 else rng.choice(ENGINEERS)
            opened = EPOCH - timedelta(hours=rng.randint(1, 720))
            subject = rng.choice(SUBJECTS).format(
                region=self.customers[cust].region.upper(),
                tier=self.customers[cust].tier,
            )
            self.tickets[tid] = Ticket(
                id=tid,
                subject=subject,
                customer_id=cust,
                priority=priority,
                status=status,
                assignee=assignee,
                opened_at=opened.isoformat(),
                tags=rng.sample(["billing", "auth", "perf", "data", "mobile"], k=rng.randint(0, 2)),
            )

        for i in range(30):
            sku = f"SKU-{300 + i}"
            reorder = rng.choice([10, 20, 25, 50])
            target = reorder * rng.choice([3, 4, 5])
            # Roughly a fifth sit below the reorder point, so the restock task has work.
            on_hand = (
                rng.randint(0, reorder - 1) if rng.random() < 0.2 else rng.randint(reorder, target)
            )
            self.inventory[sku] = InventoryItem(
                sku=sku,
                name=_part_name(rng, i),
                on_hand=on_hand,
                reorder_point=reorder,
                target_level=target,
                warehouse=rng.choice(["wh-north", "wh-south", "wh-east"]),
            )

        self._log("system", "seed", "world", f"seeded with BENCH_SEED={SEED}")

    # -- audit -------------------------------------------------------------

    def _log(self, actor: str, action: str, target: str, detail: str) -> None:
        self._audit_seq += 1
        self.audit.append(
            AuditEntry(
                seq=self._audit_seq,
                # Derived from the sequence number, not wall clock, so runs are comparable.
                at=(EPOCH + timedelta(seconds=self._audit_seq)).isoformat(),
                actor=actor,
                action=action,
                target=target,
                detail=detail,
            )
        )

    # -- queries -----------------------------------------------------------

    def list_tickets(
        self,
        status: str | None = None,
        priority: str | None = None,
        assignee: str | None = None,
        customer_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = list(self.tickets.values())
        if status:
            rows = [t for t in rows if t.status == status]
        if priority:
            rows = [t for t in rows if t.priority == priority]
        if assignee is not None:
            rows = [t for t in rows if (t.assignee or "") == assignee]
        if customer_id:
            rows = [t for t in rows if t.customer_id == customer_id]
        rows.sort(key=lambda t: t.opened_at)
        return [asdict(t) for t in rows[:limit]]

    def list_customers(self, q: str | None = None, tier: str | None = None) -> list[dict[str, Any]]:
        rows = list(self.customers.values())
        if q:
            needle = q.lower()
            rows = [c for c in rows if needle in c.name.lower() or needle in c.id.lower()]
        if tier:
            rows = [c for c in rows if c.tier == tier]
        rows.sort(key=lambda c: c.id)
        return [asdict(c) for c in rows]

    def list_inventory(self, below_reorder: bool = False, warehouse: str | None = None) -> list[dict[str, Any]]:
        rows = list(self.inventory.values())
        if below_reorder:
            rows = [i for i in rows if i.on_hand < i.reorder_point]
        if warehouse:
            rows = [i for i in rows if i.warehouse == warehouse]
        rows.sort(key=lambda i: i.sku)
        return [asdict(i) for i in rows]

    # -- mutations ---------------------------------------------------------

    def assign_ticket(self, ticket_id: str, assignee: str) -> dict[str, Any]:
        t = self.tickets[ticket_id]
        before = t.assignee
        t.assignee = assignee
        self._log("agent", "assign_ticket", ticket_id, f"{before} -> {assignee}")
        return asdict(t)

    def set_ticket_priority(self, ticket_id: str, priority: str) -> dict[str, Any]:
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        t = self.tickets[ticket_id]
        before = t.priority
        t.priority = priority
        self._log("agent", "set_ticket_priority", ticket_id, f"{before} -> {priority}")
        return asdict(t)

    def set_ticket_status(self, ticket_id: str, status: str) -> dict[str, Any]:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        t = self.tickets[ticket_id]
        before = t.status
        t.status = status
        self._log("agent", "set_ticket_status", ticket_id, f"{before} -> {status}")
        return asdict(t)

    def adjust_inventory(self, sku: str, delta: int) -> dict[str, Any]:
        item = self.inventory[sku]
        before = item.on_hand
        item.on_hand = max(0, item.on_hand + delta)
        self._log("agent", "adjust_inventory", sku, f"{before} -> {item.on_hand}")
        return asdict(item)


def _company_name(rng: random.Random, i: int) -> str:
    first = ["Northwind", "Kestrel", "Harbour", "Ironwood", "Vantage", "Solstice",
             "Meridian", "Calder", "Brightwater", "Ravenscroft", "Aldgate", "Pinehurst"]
    second = ["Logistics", "Systems", "Analytics", "Foods", "Media", "Health",
              "Robotics", "Energy", "Retail", "Labs", "Freight", "Financial"]
    return f"{first[i % len(first)]} {second[(i * 7) % len(second)]}"


def _part_name(rng: random.Random, i: int) -> str:
    kind = ["Bracket", "Sensor", "Coupler", "Gasket", "Actuator", "Harness",
            "Manifold", "Bearing", "Relay", "Filter"]
    size = ["A", "B", "C", "D", "E", "F"]
    return f"{kind[i % len(kind)]} {size[(i * 3) % len(size)]}{100 + i}"
