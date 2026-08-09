"""Distractor tool generator for the tool-count sweep.

The point of the sweep is to answer, with a number, whether an agent can be pointed at a
large shared MCP gateway. So the distractors have to be *plausible*: tools a real
enterprise gateway would expose, with realistic schemas and realistic description weight.
Obvious junk tools would make the model's job artificially easy and the result useless.

None of them touch the ops backend. Any call to one is, by construction, a wrong-tool
event.
"""

from __future__ import annotations

import random
from typing import Any

from ..arms.base import ToolSpec

DOMAINS: list[tuple[str, list[tuple[str, str, dict[str, str]]]]] = [
    (
        "hr",
        [
            ("list_employees", "List employee records in a department, optionally filtered by employment status and location.", {"department": "string", "status": "string", "location": "string"}),
            ("get_employee", "Retrieve the full personnel record for a single employee by their internal identifier.", {"employee_id": "string"}),
            ("submit_timeoff", "Submit a time-off request on behalf of an employee for a given date range and leave category.", {"employee_id": "string", "start_date": "string", "end_date": "string", "category": "string"}),
            ("list_openings", "List currently published job openings for a business unit, including headcount and hiring manager.", {"business_unit": "string", "level": "string"}),
            ("get_org_chart", "Return the reporting chain above and below a given employee, to a specified depth.", {"employee_id": "string", "depth": "integer"}),
        ],
    ),
    (
        "finance",
        [
            ("list_invoices", "List invoices for a billing account, filtered by settlement status and issue date range.", {"account_id": "string", "status": "string", "since": "string"}),
            ("get_invoice", "Retrieve a single invoice document including line items, tax treatment and payment history.", {"invoice_id": "string"}),
            ("create_purchase_order", "Raise a purchase order against a supplier for an approved cost centre.", {"supplier_id": "string", "cost_centre": "string", "amount": "number"}),
            ("get_budget", "Return committed, forecast and remaining budget for a cost centre in a fiscal period.", {"cost_centre": "string", "period": "string"}),
            ("list_expense_claims", "List submitted expense claims awaiting approval for a given approver.", {"approver": "string", "status": "string"}),
        ],
    ),
    (
        "calendar",
        [
            ("list_events", "List calendar events for a principal within a time window, including declined invitations.", {"principal": "string", "start": "string", "end": "string"}),
            ("create_event", "Create a calendar event and invite the listed attendees, optionally reserving a room.", {"title": "string", "start": "string", "duration_minutes": "integer", "attendees": "string"}),
            ("find_free_slot", "Find the earliest window in which all listed attendees are simultaneously free.", {"attendees": "string", "duration_minutes": "integer", "after": "string"}),
            ("cancel_event", "Cancel a calendar event and notify all attendees with an optional reason.", {"event_id": "string", "reason": "string"}),
        ],
    ),
    (
        "docs",
        [
            ("search_documents", "Full-text search across the document management system, scoped to a space.", {"query": "string", "space": "string", "limit": "integer"}),
            ("get_document", "Fetch a document's current revision, metadata and access control list.", {"document_id": "string"}),
            ("create_page", "Create a new page in a documentation space under a parent page.", {"space": "string", "parent_id": "string", "title": "string", "body": "string"}),
            ("list_recent_edits", "List recent edits to a documentation space, newest first, with author attribution.", {"space": "string", "limit": "integer"}),
        ],
    ),
    (
        "crm_marketing",
        [
            ("list_campaigns", "List marketing campaigns filtered by channel and lifecycle stage.", {"channel": "string", "stage": "string"}),
            ("get_lead_score", "Return the current model-derived lead score and its contributing factors.", {"lead_id": "string"}),
            ("enroll_in_sequence", "Enrol a contact into an automated outreach sequence starting at a given step.", {"contact_id": "string", "sequence_id": "string", "step": "integer"}),
            ("list_attribution", "Return multi-touch attribution for a closed opportunity across marketing touchpoints.", {"opportunity_id": "string"}),
        ],
    ),
    (
        "infra",
        [
            ("list_clusters", "List compute clusters in a region with node counts and current utilisation.", {"region": "string", "environment": "string"}),
            ("scale_deployment", "Change the replica count of a deployment in a namespace.", {"namespace": "string", "deployment": "string", "replicas": "integer"}),
            ("get_metric", "Query a named time-series metric over a window with a chosen aggregation.", {"metric": "string", "window": "string", "aggregation": "string"}),
            ("list_alerts", "List currently firing alerts for a service, including severity and runbook link.", {"service": "string", "severity": "string"}),
            ("acknowledge_alert", "Acknowledge a firing alert and suppress re-notification for a period.", {"alert_id": "string", "minutes": "integer"}),
        ],
    ),
    (
        "security",
        [
            ("list_access_grants", "List standing access grants held by a principal across connected systems.", {"principal": "string", "system": "string"}),
            ("revoke_grant", "Revoke a specific access grant and record the justification.", {"grant_id": "string", "justification": "string"}),
            ("list_vulnerabilities", "List open vulnerability findings for a repository, filtered by severity.", {"repository": "string", "severity": "string"}),
            ("get_compliance_status", "Return the current control status for a compliance framework and scope.", {"framework": "string", "scope": "string"}),
        ],
    ),
]


# Near-miss distractors: same domain, overlapping vocabulary, plausibly-correct-looking
# names for the ops tasks in this suite. This is the hard version of the sweep. The
# cross-domain set above asks "can the model tell ops from HR", which turns out to be easy.
# This set asks "can the model tell `list_tickets` from `list_service_requests`", which is
# the discrimination a real enterprise gateway actually demands, because a real gateway
# exposes six systems that all model roughly the same nouns.
NEAR_MISS: list[tuple[str, str, dict[str, str]]] = [
    ("list_incidents", "List operational incidents, filtered by severity and current state.", {"severity": "string", "state": "string", "limit": "integer"}),
    ("get_incident", "Retrieve a single operational incident record by its identifier.", {"incident_id": "string"}),
    ("assign_incident", "Assign an operational incident to a responder.", {"incident_id": "string", "responder": "string"}),
    ("list_service_requests", "List service requests raised through the service desk, filtered by state and requester.", {"state": "string", "requester": "string"}),
    ("get_service_request", "Retrieve a single service desk request by its identifier.", {"request_id": "string"}),
    ("set_request_priority", "Change the priority of a service desk request.", {"request_id": "string", "priority": "string"}),
    ("list_cases", "List support cases in the legacy case management system, filtered by status.", {"status": "string", "owner": "string"}),
    ("escalate_case", "Escalate a support case to the next tier and notify the owner.", {"case_id": "string", "tier": "string"}),
    ("list_accounts", "List customer accounts in the billing system, filtered by plan and territory.", {"plan": "string", "territory": "string"}),
    ("get_account", "Retrieve a single billing account record by its identifier.", {"account_id": "string"}),
    ("list_stock_levels", "List current stock levels per item across distribution centres.", {"centre": "string", "low_only": "string"}),
    ("update_stock_level", "Set the absolute stock level for an item at a distribution centre.", {"item_code": "string", "level": "integer"}),
    ("list_parts", "List parts in the engineering parts catalogue with their reorder thresholds.", {"category": "string", "below_threshold": "string"}),
    ("adjust_part_count", "Apply a signed adjustment to a part's counted quantity.", {"part_code": "string", "change": "integer"}),
    ("list_engineers", "List engineers in the support organisation and their current rotation.", {"team": "string", "rotation": "string"}),
    ("get_rota", "Return the published on-call rota for a team over a date range.", {"team": "string", "start": "string", "end": "string"}),
    ("list_change_records", "List change records raised against production services.", {"service": "string", "window": "string"}),
    ("get_activity_log", "Return the recent activity log for a subject, newest first.", {"subject": "string", "limit": "integer"}),
]


def generate_near_miss(count: int, seed: int = 1729) -> list[ToolSpec]:
    """Same-domain distractors that overlap semantically with the real ops tools."""
    if count <= 0:
        return []
    rng = random.Random(seed)
    pool = list(NEAR_MISS)
    rng.shuffle(pool)
    out: list[ToolSpec] = []
    i = 0
    while len(out) < count:
        name, desc, params = pool[i % len(pool)]
        generation = i // len(pool)
        tool_name = name if generation == 0 else f"{name}_v{generation + 1}"
        out.append(
            ToolSpec(
                name=tool_name,
                description=desc,
                input_schema={
                    "type": "object",
                    "properties": {k: {"type": v} for k, v in params.items()},
                    "required": [next(iter(params))] if params else [],
                    "additionalProperties": False,
                },
            )
        )
        i += 1
    return out


NEAR_MISS_NAMES = {n for n, _, _ in NEAR_MISS}


def generate(count: int, seed: int = 1729) -> list[ToolSpec]:
    """Return `count` plausible, irrelevant tools, deterministically.

    Cycles through domains so a small count is not accidentally all-HR, and suffixes
    names once the templates are exhausted so arbitrarily large counts stay unique.
    """
    if count <= 0:
        return []

    rng = random.Random(seed)
    flat: list[tuple[str, str, str, dict[str, str]]] = []
    for domain, tools in DOMAINS:
        for name, desc, params in tools:
            flat.append((domain, name, desc, params))
    rng.shuffle(flat)

    out: list[ToolSpec] = []
    i = 0
    while len(out) < count:
        domain, name, desc, params = flat[i % len(flat)]
        generation = i // len(flat)
        tool_name = f"{domain}_{name}" if generation == 0 else f"{domain}_{name}_v{generation + 1}"
        out.append(
            ToolSpec(
                name=tool_name,
                description=desc,
                input_schema={
                    "type": "object",
                    "properties": {k: {"type": v} for k, v in params.items()},
                    "required": [next(iter(params))] if params else [],
                    "additionalProperties": False,
                },
            )
        )
        i += 1
    return out


def is_distractor(tool_name: str) -> bool:
    """True if a tool name belongs to either distractor set (cross-domain or near-miss)."""
    if any(tool_name.startswith(f"{domain}_") for domain, _ in DOMAINS):
        return True
    base = tool_name.rsplit("_v", 1)[0] if "_v" in tool_name else tool_name
    return base in NEAR_MISS_NAMES
