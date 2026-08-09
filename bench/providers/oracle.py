"""Oracle provider: a scripted perfect agent. Not a model.

This exists so the harness can be tested end to end without an API key or a dollar of
spend: it exercises the agent loop, tool dispatch, state reset, grading, token accounting
and the report renderer. It is also the ceiling, which is genuinely useful. If a task
fails under the oracle, the task or the arm is broken, not the model.

It is NOT a baseline to compare models against, and the report will say so. It always
solves every task by construction, so a success rate of 100% here means the plumbing
works and nothing else.

Token usage is synthetic but structurally faithful: the tool block is charged on every
turn, which is exactly the property the context-economics comparison turns on.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..arms.base import ToolResult, ToolSpec
from .base import ToolCall, TurnResult, Usage

# Rough characters-per-token. Only used to give the harness plausible numbers to
# aggregate; never presented as a measurement.
CHARS_PER_TOKEN = 3.6


class OracleProvider:
    name = "oracle"
    supports_explicit_cache = False

    def __init__(self, model: str | None = None) -> None:
        self.model = model or "oracle-v1"

    def new_conversation(self, system: str, user: str) -> dict[str, Any]:
        return {"system": system, "user": user, "step": 0, "facts": {}, "history": []}

    async def turn(
        self,
        conversation: dict[str, Any],
        tools: list[ToolSpec],
        cache_tools: bool = False,
    ) -> TurnResult:
        t0 = time.perf_counter()
        plan = _plan_for(conversation["user"])
        calls, text = plan(conversation)
        conversation["step"] += 1

        tool_chars = sum(len(json.dumps(t.to_anthropic())) for t in tools)
        history_chars = sum(len(str(h)) for h in conversation["history"])
        usage = Usage(
            input_tokens=int((tool_chars + history_chars + len(conversation["user"])) / CHARS_PER_TOKEN),
            output_tokens=int(max(20, len(text) / CHARS_PER_TOKEN)),
        )
        return TurnResult(
            text=text,
            tool_calls=calls,
            usage=usage,
            stop_reason="tool_use" if calls else "end_turn",
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def add_tool_results(
        self,
        conversation: dict[str, Any],
        turn: TurnResult,
        results: list[ToolResult],
    ) -> None:
        for call, result in zip(turn.tool_calls, results):
            conversation["history"].append({"tool": call.name, "result": result.content})
            try:
                conversation["facts"][call.name] = json.loads(result.content)
            except ValueError:
                conversation["facts"][call.name] = result.content

    async def count_tool_tokens(self, tools: list[ToolSpec]) -> int:
        return int(sum(len(json.dumps(t.to_anthropic())) for t in tools) / CHARS_PER_TOKEN)

    async def aclose(self) -> None:
        return None


# --- scripted plans ----------------------------------------------------------


def _c(name: str, **args: Any) -> ToolCall:
    return ToolCall(id=f"call_{name}_{len(args)}", name=name, args=args)


def _plan_for(prompt: str):
    p = prompt.lower()
    if "on call" in p:
        return _plan_triage
    if "reorder point" in p:
        return _plan_restock
    if "greatest number of currently open tickets" in p:
        return _plan_escalate
    if "no assignee" in p:
        return _plan_unassigned
    if "licensed seats" in p:
        return _plan_emea
    return lambda conv: ([], "I do not have a plan for this task.")


def _plan_triage(conv: dict[str, Any]) -> tuple[list[ToolCall], str]:
    step, facts = conv["step"], conv["facts"]
    if step == 0:
        return [_c("get_oncall"), _c("list_tickets", status="open", priority="P1", limit=200)], ""
    if step == 1:
        oncall = facts.get("get_oncall", {}).get("oncall")
        tickets = facts.get("list_tickets", {}).get("tickets", [])
        calls = [_c("assign_ticket", ticket_id=t["id"], assignee=oncall) for t in tickets]
        return (calls, "") if calls else ([], "No open P1 tickets to assign.")
    return [], "Assigned every open P1 ticket to the on-call engineer."


def _plan_restock(conv: dict[str, Any]) -> tuple[list[ToolCall], str]:
    step, facts = conv["step"], conv["facts"]
    if step == 0:
        return [_c("list_inventory", below_reorder=True)], ""
    if step == 1:
        items = facts.get("list_inventory", {}).get("items", [])
        calls = [
            _c("adjust_inventory", sku=i["sku"], delta=i["target_level"] - i["on_hand"])
            for i in items
        ]
        return (calls, "") if calls else ([], "Nothing below its reorder point.")
    return [], "Restocked every below-reorder SKU to its target level."


def _plan_escalate(conv: dict[str, Any]) -> tuple[list[ToolCall], str]:
    step, facts = conv["step"], conv["facts"]
    if step == 0:
        return [_c("list_tickets", status="open", limit=200)], ""
    if step == 1:
        tickets = facts.get("list_tickets", {}).get("tickets", [])
        if not tickets:
            return [], "No open tickets."
        counts: dict[str, int] = {}
        for t in tickets:
            counts[t["customer_id"]] = counts.get(t["customer_id"], 0) + 1
        top = max(counts.values())
        # Deterministic tie-break; the checker accepts any tied customer.
        customer = sorted(c for c, n in counts.items() if n == top)[0]
        theirs = sorted(
            (t for t in tickets if t["customer_id"] == customer), key=lambda t: t["opened_at"]
        )
        target = theirs[0]["id"]
        conv["facts"]["_target"] = target
        return [
            _c("set_ticket_priority", ticket_id=target, priority="P1"),
            _c("set_ticket_status", ticket_id=target, status="in_progress"),
        ], ""
    return [], f"Escalated {conv['facts'].get('_target')}."


def _plan_unassigned(conv: dict[str, Any]) -> tuple[list[ToolCall], str]:
    step, facts = conv["step"], conv["facts"]
    if step == 0:
        return [_c("list_tickets", status="open", assignee="", limit=200)], ""
    n = facts.get("list_tickets", {}).get("count", 0)
    return [], f"Open tickets with no assignee: {n}"


def _plan_emea(conv: dict[str, Any]) -> tuple[list[ToolCall], str]:
    step, facts = conv["step"], conv["facts"]
    if step == 0:
        return [_c("list_customers", tier="enterprise")], ""
    rows = [c for c in facts.get("list_customers", {}).get("customers", []) if c["region"] == "emea"]
    if not rows:
        return [], "No enterprise customers in EMEA."
    best = max(rows, key=lambda c: c["seats"])
    return [], f"The largest enterprise EMEA customer by seats is {best['id']}"
