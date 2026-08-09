"""Graded task suite with deterministic checkers.

Design rules, so grading never becomes a judgement call:

* Mutating tasks are graded on backend state, never on what the model said it did.
* Read-only tasks are graded on a single extractable value, never on prose quality.
* Every checker also verifies that nothing *else* changed. An agent that assigns the right
  tickets and also resolves twenty unrelated ones has not passed.
* Where the correct answer could tie, the checker accepts any member of the tied set.
  Silent tie-breaking is how a benchmark starts scoring luck.

`expected()` derives ground truth from a fresh seeded World rather than hard-coding it, so
changing BENCH_SEED changes the answers and the checkers stay correct.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from ..backend.data import ONCALL, World


@dataclass
class Verdict:
    passed: bool
    detail: str


@dataclass
class Task:
    id: str
    prompt: str
    #: Tools this task genuinely needs. Drives mcp_filtered and the wrong-tool metric.
    relevant_tools: list[str]
    check: Callable[[dict[str, Any], str], Verdict]
    mutating: bool = True
    tags: list[str] = field(default_factory=list)
    #: How the same task sounds when a person asks for it rather than a spec writer.
    #:
    #: The explicit prompts name their domain outright ("support ticket", "priority P1",
    #: "reorder point"), so a model can route to the right tool on keyword overlap alone
    #: without ever discriminating between similar tools. That makes the distractor sweep
    #: much easier than reality and is the single most likely reason tool-count
    #: degradation did not reproduce. These variants remove the giveaway vocabulary while
    #: keeping the task, and therefore the checker, identical.
    prompt_vague: str = ""

    def text(self, phrasing: str = "explicit") -> str:
        if phrasing == "vague" and self.prompt_vague:
            return self.prompt_vague
        return self.prompt


def baseline() -> World:
    """A fresh world at the seed. Ground truth for every checker."""
    return World()


# --- helpers -----------------------------------------------------------------


def _tickets(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return state["tickets"]


def _inventory(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return state["inventory"]


def _last_number(text: str) -> int | None:
    nums = re.findall(r"-?\d+", text or "")
    return int(nums[-1]) if nums else None


def _unchanged_tickets(
    state: dict[str, Any], base: World, ignore: set[str], fields: tuple[str, ...]
) -> str | None:
    """Return a description of the first unexpected ticket mutation, or None."""
    for tid, want in base.tickets.items():
        if tid in ignore:
            continue
        got = _tickets(state).get(tid)
        if got is None:
            return f"ticket {tid} disappeared"
        for f in fields:
            if got.get(f) != getattr(want, f):
                return f"ticket {tid}.{f} changed unexpectedly ({getattr(want, f)!r} -> {got.get(f)!r})"
    return None


# --- task 1: triage open P1 to on-call ---------------------------------------


def _check_triage(state: dict[str, Any], _text: str) -> Verdict:
    base = baseline()
    targets = {
        tid for tid, t in base.tickets.items() if t.status == "open" and t.priority == "P1"
    }
    if not targets:
        return Verdict(False, "seed produced no open P1 tickets; task is degenerate")

    missed = [tid for tid in sorted(targets) if _tickets(state)[tid]["assignee"] != ONCALL]
    if missed:
        return Verdict(False, f"not assigned to {ONCALL}: {', '.join(missed)}")

    drift = _unchanged_tickets(state, base, ignore=targets, fields=("assignee", "priority", "status"))
    if drift:
        return Verdict(False, f"collateral change: {drift}")
    return Verdict(True, f"assigned {len(targets)} open P1 tickets to {ONCALL}")


TRIAGE = Task(
    id="triage_oncall",
    prompt=(
        "Every support ticket that is currently open and priority P1 must be assigned to "
        "the engineer who is on call right now. Find them and assign them. Do not change "
        "anything else about any ticket."
    ),
    relevant_tools=["get_oncall", "list_tickets", "assign_ticket"],
    check=_check_triage,
    tags=["mutate", "multi_hop"],
    prompt_vague=(
        "Nothing critical should be sitting there unowned. Whoever is carrying the pager "
        "on this rotation needs to own everything urgent that still hasn't been picked "
        "up. Leave everything else exactly as it is."
    ),
)


# --- task 2: restock below-reorder SKUs --------------------------------------


def _check_restock(state: dict[str, Any], _text: str) -> Verdict:
    base = baseline()
    low = {sku: it for sku, it in base.inventory.items() if it.on_hand < it.reorder_point}
    if not low:
        return Verdict(False, "seed produced no below-reorder SKUs; task is degenerate")

    wrong = []
    for sku, want in low.items():
        got = _inventory(state)[sku]["on_hand"]
        if got != want.target_level:
            wrong.append(f"{sku} at {got}, expected {want.target_level}")
    if wrong:
        return Verdict(False, "; ".join(wrong[:5]))

    for sku, want in base.inventory.items():
        if sku in low:
            continue
        if _inventory(state)[sku]["on_hand"] != want.on_hand:
            return Verdict(False, f"collateral change: {sku} stock moved but was not low")
    return Verdict(True, f"restocked {len(low)} SKUs to target")


RESTOCK = Task(
    id="restock_low_stock",
    prompt=(
        "Any SKU whose on-hand quantity has fallen below its reorder point must be "
        "restocked to exactly its target level. Find those SKUs and adjust their stock. "
        "Leave every other SKU alone."
    ),
    relevant_tools=["list_inventory", "adjust_inventory"],
    check=_check_restock,
    tags=["mutate", "arithmetic"],
    prompt_vague=(
        "The warehouse says we have run short on a few lines. Anything that has dipped "
        "under its trigger level needs bringing back up to where it is supposed to sit. "
        "Do not touch the lines that are fine."
    ),
)


# --- task 3: escalate the noisiest customer's oldest open ticket -------------


def _check_escalate(state: dict[str, Any], _text: str) -> Verdict:
    base = baseline()
    open_by_cust = Counter(t.customer_id for t in base.tickets.values() if t.status == "open")
    if not open_by_cust:
        return Verdict(False, "seed produced no open tickets; task is degenerate")

    top = max(open_by_cust.values())
    tied = {c for c, n in open_by_cust.items() if n == top}

    # Any tied customer is acceptable; within one, the oldest open ticket is unambiguous.
    acceptable = set()
    for cust in tied:
        theirs = sorted(
            (t for t in base.tickets.values() if t.customer_id == cust and t.status == "open"),
            key=lambda t: t.opened_at,
        )
        if theirs:
            acceptable.add(theirs[0].id)

    hits = [
        tid
        for tid in acceptable
        if _tickets(state)[tid]["priority"] == "P1"
        and _tickets(state)[tid]["status"] == "in_progress"
    ]
    if not hits:
        return Verdict(
            False,
            f"no acceptable ticket escalated. Acceptable: {sorted(acceptable)}",
        )

    drift = _unchanged_tickets(state, base, ignore=set(hits), fields=("priority", "status"))
    if drift:
        return Verdict(False, f"collateral change: {drift}")
    return Verdict(True, f"escalated {hits[0]} (tied-max customers: {sorted(tied)})")


ESCALATE = Task(
    id="escalate_top_customer",
    prompt=(
        "Find the customer with the greatest number of currently open tickets. Take that "
        "customer's oldest open ticket, raise it to priority P1, and set its status to "
        "in_progress. Change nothing else."
    ),
    relevant_tools=[
        "list_tickets",
        "list_customers",
        "set_ticket_priority",
        "set_ticket_status",
    ],
    check=_check_escalate,
    tags=["mutate", "aggregate", "multi_hop"],
    prompt_vague=(
        "One of our accounts is generating far more noise than any of the others at the "
        "moment. Work out which one, then take the longest-running thing they still have "
        "outstanding, flag it at the top urgency, and show it as actively being worked "
        "on. Change nothing else."
    ),
)


# --- task 4: count unassigned open tickets (read-only) -----------------------


def _check_unassigned(state: dict[str, Any], text: str) -> Verdict:
    base = baseline()
    expected = sum(1 for t in base.tickets.values() if t.status == "open" and not t.assignee)
    got = _last_number(text)
    if got is None:
        return Verdict(False, "no number found in the final response")
    if got != expected:
        return Verdict(False, f"answered {got}, expected {expected}")

    drift = _unchanged_tickets(state, base, ignore=set(), fields=("assignee", "priority", "status"))
    if drift:
        return Verdict(False, f"read-only task mutated state: {drift}")
    return Verdict(True, f"answered {expected}")


UNASSIGNED = Task(
    id="count_unassigned_open",
    prompt=(
        "How many tickets are currently open and have no assignee? Reply with a short "
        "sentence, and make the final characters of your reply the number itself."
    ),
    relevant_tools=["list_tickets"],
    check=_check_unassigned,
    mutating=False,
    tags=["read_only", "count"],
    prompt_vague=(
        "How many things are sitting in the queue right now with nobody's name against "
        "them? Reply with a short sentence, and make the final characters of your reply "
        "the number itself."
    ),
)


# --- task 5: largest enterprise EMEA customer (read-only, multi-hop) ---------


def _check_largest_emea(state: dict[str, Any], text: str) -> Verdict:
    base = baseline()
    pool = [c for c in base.customers.values() if c.tier == "enterprise" and c.region == "emea"]
    if not pool:
        return Verdict(False, "seed produced no enterprise EMEA customers; task is degenerate")
    top = max(c.seats for c in pool)
    acceptable = {c.id for c in pool if c.seats == top}

    said = set(re.findall(r"CUST-\d+", text or ""))
    if not said:
        return Verdict(False, "no customer id found in the final response")
    # Take the last mentioned id, so a reasoning trail that names several is graded on
    # its conclusion rather than on anything it considered along the way.
    last = re.findall(r"CUST-\d+", text)[-1]
    if last not in acceptable:
        return Verdict(False, f"answered {last}, acceptable: {sorted(acceptable)}")

    drift = _unchanged_tickets(state, base, ignore=set(), fields=("assignee", "priority", "status"))
    if drift:
        return Verdict(False, f"read-only task mutated state: {drift}")
    return Verdict(True, f"answered {last}")


LARGEST_EMEA = Task(
    id="largest_enterprise_emea",
    prompt=(
        "Among customers on the enterprise tier in the EMEA region, which one has the "
        "most licensed seats? Reply with a short sentence ending in that customer's "
        "identifier."
    ),
    relevant_tools=["list_customers", "get_customer"],
    check=_check_largest_emea,
    mutating=False,
    tags=["read_only", "filter", "aggregate"],
    prompt_vague=(
        "Of our top-plan accounts based out of Europe, the Middle East and Africa, which "
        "is the largest by headcount? Reply with a short sentence ending in their "
        "identifier."
    ),
)


TASKS: list[Task] = [TRIAGE, RESTOCK, ESCALATE, UNASSIGNED, LARGEST_EMEA]
TASKS_BY_ID = {t.id: t for t in TASKS}


def validate_suite() -> list[str]:
    """Check the seed produces a non-degenerate instance of every task.

    Run this after changing BENCH_SEED. A task whose answer set is empty silently scores
    zero for every arm, which looks like a finding and is not one.
    """
    problems: list[str] = []
    base = baseline()
    state = {
        "tickets": {k: vars(v) for k, v in base.tickets.items()},
        "customers": {k: vars(v) for k, v in base.customers.items()},
        "inventory": {k: vars(v) for k, v in base.inventory.items()},
    }
    for task in TASKS:
        verdict = task.check(state, "")
        # An unsolved task must fail for a *solvable* reason, not a degenerate one.
        if "degenerate" in verdict.detail:
            problems.append(f"{task.id}: {verdict.detail}")
    return problems
