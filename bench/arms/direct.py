"""The `direct` arm: hand-authored tool schemas calling the REST API in-process.

This is the architects' baseline. Note what makes it different from the MCP arms beyond
the missing hop: the schemas below are written by someone who knows exactly which agent
will consume them and which three tools that agent needs. They are terser than what a
general-purpose MCP server advertises, and that terseness is a real, repeatable advantage
worth measuring rather than normalising away.

The cost of that advantage is that this file has to be written and maintained once per
(agent, backend) pair. That is the N x M term the crossover model prices.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

from .base import Arm, ToolResult, ToolSpec

BACKEND = os.getenv("BACKEND_URL", "http://127.0.0.1:9110").rstrip("/")


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


# Hand-authored schemas. Compare the description lengths against bench/mcpserver/server.py.
TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="list_tickets",
        description="List support tickets. Filter by status, priority, assignee or customer.",
        input_schema=_obj(
            {
                "status": {"type": "string", "enum": ["open", "in_progress", "blocked", "resolved"]},
                "priority": {"type": "string", "enum": ["P1", "P2", "P3", "P4"]},
                "assignee": {"type": "string", "description": "Username, or empty string for unassigned"},
                "customer_id": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            }
        ),
    ),
    ToolSpec(
        name="get_ticket",
        description="Get one ticket by id.",
        input_schema=_obj({"ticket_id": {"type": "string"}}, ["ticket_id"]),
    ),
    ToolSpec(
        name="assign_ticket",
        description="Assign a ticket to an engineer.",
        input_schema=_obj(
            {"ticket_id": {"type": "string"}, "assignee": {"type": "string"}},
            ["ticket_id", "assignee"],
        ),
    ),
    ToolSpec(
        name="set_ticket_priority",
        description="Change a ticket's priority.",
        input_schema=_obj(
            {
                "ticket_id": {"type": "string"},
                "priority": {"type": "string", "enum": ["P1", "P2", "P3", "P4"]},
            },
            ["ticket_id", "priority"],
        ),
    ),
    ToolSpec(
        name="set_ticket_status",
        description="Change a ticket's status.",
        input_schema=_obj(
            {
                "ticket_id": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "in_progress", "blocked", "resolved"]},
            },
            ["ticket_id", "status"],
        ),
    ),
    ToolSpec(
        name="list_customers",
        description="List customers. Optional name search and tier filter.",
        input_schema=_obj(
            {
                "q": {"type": "string"},
                "tier": {"type": "string", "enum": ["enterprise", "business", "starter"]},
            }
        ),
    ),
    ToolSpec(
        name="get_customer",
        description="Get one customer by id.",
        input_schema=_obj({"customer_id": {"type": "string"}}, ["customer_id"]),
    ),
    ToolSpec(
        name="list_inventory",
        description="List SKUs. Set below_reorder to find items needing restock.",
        input_schema=_obj(
            {
                "below_reorder": {"type": "boolean", "default": False},
                "warehouse": {"type": "string", "enum": ["wh-north", "wh-south", "wh-east"]},
            }
        ),
    ),
    ToolSpec(
        name="adjust_inventory",
        description="Apply a signed change to a SKU's on-hand quantity.",
        input_schema=_obj(
            {"sku": {"type": "string"}, "delta": {"type": "integer"}}, ["sku", "delta"]
        ),
    ),
    ToolSpec(
        name="list_audit",
        description="Read the most recent audit log entries.",
        input_schema=_obj({"limit": {"type": "integer", "default": 20}}),
    ),
    ToolSpec(
        name="get_oncall",
        description="Get the current on-call engineer username.",
        input_schema=_obj({}),
    ),
]

# name -> (http method, path template, how to place args)
_ROUTES: dict[str, tuple[str, str, str]] = {
    "list_tickets": ("GET", "/tickets", "query"),
    "get_ticket": ("GET", "/tickets/{ticket_id}", "path"),
    "assign_ticket": ("POST", "/tickets/{ticket_id}/assign", "body"),
    "set_ticket_priority": ("POST", "/tickets/{ticket_id}/priority", "body"),
    "set_ticket_status": ("POST", "/tickets/{ticket_id}/status", "body"),
    "list_customers": ("GET", "/customers", "query"),
    "get_customer": ("GET", "/customers/{customer_id}", "path"),
    "list_inventory": ("GET", "/inventory", "query"),
    "adjust_inventory": ("POST", "/inventory/{sku}/adjust", "body"),
    "list_audit": ("GET", "/audit", "query"),
    "get_oncall": ("GET", "/oncall", "query"),
}


class DirectArm(Arm):
    name = "direct"
    transport = "in_process_http"

    def __init__(self, tool_filter: list[str] | None = None, base_url: str | None = None) -> None:
        super().__init__(tool_filter)
        self.base_url = (base_url or BACKEND).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> None:
        t0 = time.perf_counter()
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        self.session_cost.connect_ms = (time.perf_counter() - t0) * 1000.0
        # No handshake and no remote tool discovery: the schemas are compiled in. That
        # zero is the honest number, not an omission.
        self.session_cost.initialize_ms = 0.0
        self.session_cost.list_tools_ms = 0.0

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def list_tools(self) -> list[ToolSpec]:
        self._tools = self._apply_filter(TOOLS)
        return self._tools

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        if self._client is None:
            raise RuntimeError("arm not connected")
        route = _ROUTES.get(name)
        if route is None:
            return ToolResult(
                content=json.dumps({"error": f"unknown tool: {name}"}), is_error=True
            )
        method, template, placement = route
        args = dict(args)

        # Path params are consumed out of args before the rest is placed.
        path = template
        for key in list(args):
            token = "{" + key + "}"
            if token in path:
                path = path.replace(token, str(args.pop(key)))

        kwargs: dict[str, Any] = {}
        if placement == "query" or (placement == "path" and args):
            kwargs["params"] = {k: v for k, v in args.items() if v is not None}
        elif placement == "body":
            kwargs["json"] = args

        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            return ToolResult(content=json.dumps({"error": str(exc)}), is_error=True)

        upstream_ms = float(resp.headers.get("X-Backend-Elapsed-Ms", "0") or 0)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": resp.text}
        if resp.status_code >= 400:
            return ToolResult(
                content=json.dumps({"error": payload.get("detail", payload)}),
                upstream_ms=upstream_ms,
                is_error=True,
            )
        return ToolResult(content=json.dumps(payload), upstream_ms=upstream_ms)
