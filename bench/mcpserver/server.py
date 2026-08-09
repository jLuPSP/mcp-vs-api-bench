"""Thin MCP wrapper over the ops backend.

Deliberately thin: no caching, no batching, no business logic. Every tool is a direct
pass-through to the corresponding HTTP endpoint. If this server were smarter than the
direct arm's client code, the benchmark would be comparing implementations rather than
access layers.

Docstrings here are intentionally written the way real MCP server authors write them:
complete, self-describing, and a bit verbose, because the server has no idea which agent
will consume them. The direct arm's hand-authored schemas are tighter. That difference in
schema weight is part of what is being measured, not a confound to be normalised away.

Transports:
    python -m bench.mcpserver.server --transport stdio
    python -m bench.mcpserver.server --transport http --host 0.0.0.0 --port 9111
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import warnings
from typing import Any

# Filtered before the MCP import, because under stdio this process writes to the
# harness's stderr and the warning would land in the middle of a results table.
warnings.filterwarnings("ignore", module=r"pydantic_settings.*")
warnings.filterwarnings("ignore", message=r".*incomplete definition.*")

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

BACKEND = os.getenv("BACKEND_URL", "http://127.0.0.1:9110").rstrip("/")

# Under the stdio transport this process is a subprocess of the harness and inherits its
# stderr, so per-request INFO logging would bury the benchmark output. It also costs real
# time inside the measurement window, which would inflate the stdio arm for a reason that
# has nothing to do with MCP.
LOG_LEVEL = os.getenv("MCP_LOG_LEVEL", "WARNING").upper()
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(LOG_LEVEL)

# One pooled client for the server's lifetime. A per-call client would make the MCP arms
# look worse for a reason that has nothing to do with MCP.
_client = httpx.Client(base_url=BACKEND, timeout=30.0)


def _call(method: str, path: str, **kwargs: Any) -> str:
    """Issue the upstream request and return JSON text with upstream timing attached.

    `_bench_upstream_ms` is benchmark instrumentation. The harness reads it to subtract
    upstream service time from wall clock, then strips it before the payload is shown to
    a model, so token counts stay identical across arms.
    """
    resp = _client.request(method, path, **kwargs)
    upstream_ms = float(resp.headers.get("X-Backend-Elapsed-Ms", "0") or 0)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"error": resp.text}
    if resp.status_code >= 400:
        payload = {"error": payload.get("detail", payload), "status": resp.status_code}
    return json.dumps({"_bench_upstream_ms": upstream_ms, "result": payload})


def build_server(host: str = "0.0.0.0", port: int = 9111) -> FastMCP:
    mcp = FastMCP("ops", host=host, port=port, log_level=LOG_LEVEL)

    @mcp.tool()
    def list_tickets(
        status: str | None = None,
        priority: str | None = None,
        assignee: str | None = None,
        customer_id: str | None = None,
        limit: int = 50,
    ) -> str:
        """List support tickets from the operations ticketing system, with optional
        filtering. Returns a JSON object containing a count and an array of ticket
        records. Each ticket record includes its identifier, subject line, the identifier
        of the customer that raised it, its priority band, its current workflow status,
        the username of the engineer it is assigned to (null when unassigned), the
        timestamp at which it was opened, and any classification tags.

        Args:
            status: Restrict results to a single workflow status. Valid values are
                open, in_progress, blocked and resolved. Omit to return every status.
            priority: Restrict results to a single priority band. Valid values are
                P1, P2, P3 and P4, where P1 is the most urgent. Omit for all bands.
            assignee: Restrict results to tickets assigned to this engineer username.
                Pass an empty string to find tickets that have no assignee at all.
            customer_id: Restrict results to tickets raised by this customer identifier,
                for example CUST-1004.
            limit: Maximum number of ticket records to return. Defaults to 50.
        """
        params = {"limit": limit}
        if status is not None:
            params["status"] = status
        if priority is not None:
            params["priority"] = priority
        if assignee is not None:
            params["assignee"] = assignee
        if customer_id is not None:
            params["customer_id"] = customer_id
        return _call("GET", "/tickets", params=params)

    @mcp.tool()
    def get_ticket(ticket_id: str) -> str:
        """Retrieve the complete record for a single support ticket by its identifier.
        Returns the full ticket object including subject, customer, priority, status,
        assignee, opened timestamp and tags. Raises a not-found error if the identifier
        does not correspond to an existing ticket.

        Args:
            ticket_id: The ticket identifier, for example TKT-2013.
        """
        return _call("GET", f"/tickets/{ticket_id}")

    @mcp.tool()
    def assign_ticket(ticket_id: str, assignee: str) -> str:
        """Assign a support ticket to a named engineer, replacing any existing assignee.
        This writes to the ticketing system and appends an entry to the audit log.
        Returns the updated ticket record.

        Args:
            ticket_id: The ticket identifier to reassign, for example TKT-2013.
            assignee: The engineer username to assign the ticket to, for example
                r.okonkwo.
        """
        return _call("POST", f"/tickets/{ticket_id}/assign", json={"assignee": assignee})

    @mcp.tool()
    def set_ticket_priority(ticket_id: str, priority: str) -> str:
        """Change the priority band of an existing support ticket. This writes to the
        ticketing system and appends an entry to the audit log. Returns the updated
        ticket record. Rejects any priority value outside the permitted set.

        Args:
            ticket_id: The ticket identifier to modify, for example TKT-2013.
            priority: The new priority band. Must be one of P1, P2, P3 or P4.
        """
        return _call("POST", f"/tickets/{ticket_id}/priority", json={"priority": priority})

    @mcp.tool()
    def set_ticket_status(ticket_id: str, status: str) -> str:
        """Change the workflow status of an existing support ticket. This writes to the
        ticketing system and appends an entry to the audit log. Returns the updated
        ticket record. Rejects any status value outside the permitted set.

        Args:
            ticket_id: The ticket identifier to modify, for example TKT-2013.
            status: The new workflow status. Must be one of open, in_progress,
                blocked or resolved.
        """
        return _call("POST", f"/tickets/{ticket_id}/status", json={"status": status})

    @mcp.tool()
    def list_customers(q: str | None = None, tier: str | None = None) -> str:
        """List customer accounts from the customer relationship system, with optional
        filtering. Returns a JSON object containing a count and an array of customer
        records. Each customer record includes its identifier, company name, commercial
        tier, geographic region, the username of the account owner, and the number of
        licensed seats.

        Args:
            q: Free-text search applied to the customer name and identifier. The match
                is case-insensitive and matches on any substring.
            tier: Restrict results to a single commercial tier. Valid values are
                enterprise, business and starter.
        """
        params = {}
        if q is not None:
            params["q"] = q
        if tier is not None:
            params["tier"] = tier
        return _call("GET", "/customers", params=params)

    @mcp.tool()
    def get_customer(customer_id: str) -> str:
        """Retrieve the complete record for a single customer account by its identifier.
        Returns the full customer object including name, tier, region, account owner and
        seat count. Raises a not-found error if the identifier does not correspond to an
        existing customer.

        Args:
            customer_id: The customer identifier, for example CUST-1004.
        """
        return _call("GET", f"/customers/{customer_id}")

    @mcp.tool()
    def list_inventory(below_reorder: bool = False, warehouse: str | None = None) -> str:
        """List stock-keeping units from the inventory management system, with optional
        filtering. Returns a JSON object containing a count and an array of inventory
        records. Each record includes the SKU identifier, the part name, the quantity
        currently on hand, the reorder point below which the item should be restocked,
        the target stock level to restock up to, and the warehouse holding it.

        Args:
            below_reorder: When true, return only those items whose on-hand quantity has
                fallen strictly below their reorder point.
            warehouse: Restrict results to a single warehouse. Valid values are
                wh-north, wh-south and wh-east.
        """
        params = {"below_reorder": below_reorder}
        if warehouse is not None:
            params["warehouse"] = warehouse
        return _call("GET", "/inventory", params=params)

    @mcp.tool()
    def adjust_inventory(sku: str, delta: int) -> str:
        """Apply a signed adjustment to the on-hand quantity of a stock-keeping unit.
        A positive delta increases stock, a negative delta decreases it. The resulting
        quantity is floored at zero. This writes to the inventory system and appends an
        entry to the audit log. Returns the updated inventory record.

        Args:
            sku: The stock-keeping unit identifier to adjust, for example SKU-304.
            delta: The signed change to apply to the on-hand quantity.
        """
        return _call("POST", f"/inventory/{sku}/adjust", json={"delta": delta})

    @mcp.tool()
    def list_audit(limit: int = 20) -> str:
        """Retrieve the most recent entries from the operations audit log. Returns a JSON
        object containing a count and an array of audit entries, each with a monotonic
        sequence number, a timestamp, the acting principal, the action performed, the
        target of the action, and a human-readable detail string.

        Args:
            limit: Maximum number of audit entries to return, most recent last.
        """
        return _call("GET", "/audit", params={"limit": limit})

    @mcp.tool()
    def get_oncall() -> str:
        """Return the engineer username currently designated as the on-call responder for
        the operations rotation. Takes no arguments.
        """
        return _call("GET", "/oncall")

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="ops MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "9111")))
    args = parser.parse_args()

    server = build_server(host=args.host, port=args.port)
    server.run(transport="stdio" if args.transport == "stdio" else "streamable-http")


if __name__ == "__main__":
    main()
