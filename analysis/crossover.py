"""N agents x M systems economics: where MCP starts paying for itself, and where it stops.

The benchmark measures the per-request cost of MCP. This module prices the other side of
the ledger: the integration cost MCP avoids. Together they give a crossover point, which
is the only form of this answer that survives contact with a real organisation, because
the answer genuinely differs by request volume and integration count.

The model:

    direct:  A x S bespoke integrations, each built and maintained by hand
    mcp:     A + S integrations (one client per agent, one server per system)

Per-request, MCP costs extra latency and extra context tokens. Per-integration, it saves
build and maintenance effort. Everything below is stated in dollars per year so the two
sides are comparable.

Run:
    python -m analysis.crossover
    python -m analysis.crossover --agents 8 --systems 12 --requests-per-month 2000000
    python -m analysis.crossover --from-results results/agentic-<stamp>.jsonl
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class Inputs:
    # Scale
    agents: int = 5
    systems: int = 6
    requests_per_month: int = 500_000

    # Integration economics (the term that favours MCP)
    hours_per_integration: float = 24.0
    maintenance_hours_per_integration_per_year: float = 12.0
    loaded_hourly_rate: float = 140.0
    integration_lifetime_years: float = 3.0

    # Per-request economics (the term that favours direct)
    extra_tokens_per_request: int = 1800      # measured schema delta, mcp minus direct
    input_cost_per_mtok: float = 0.27         # deepseek-chat cache-miss rate
    cache_hit_fraction: float = 0.0           # 0 = nothing cached, 1 = fully cached
    # Must match the provider whose input rate is set above. 0.07/0.27 = 0.259 for
    # deepseek-chat. This defaulted to 0.1 (Anthropic's read multiplier) against a
    # DeepSeek input rate, mixing two providers' pricing and understating cached token
    # cost by 2.6x. If you switch input_cost_per_mtok to an Anthropic rate, set this to
    # 0.1 to match.
    cache_read_multiplier: float = 0.259

    # Governance (the term nobody prices, and the one that often decides it)
    policy_changes_per_year: float = 4.0
    hours_per_policy_change_direct: float = 3.0   # per integration touched
    hours_per_policy_change_mcp: float = 3.0      # once, at the gateway

    # Infrastructure
    mcp_gateway_cost_per_year: float = 6_000.0


@dataclass
class Outcome:
    direct_build_usd: float
    direct_maintain_usd: float
    direct_policy_usd: float
    direct_token_usd: float
    direct_total_usd: float

    mcp_build_usd: float
    mcp_maintain_usd: float
    mcp_policy_usd: float
    mcp_token_usd: float
    mcp_infra_usd: float
    mcp_total_usd: float

    integrations_direct: int
    integrations_mcp: int
    annual_delta_usd: float
    crossover_requests_per_month: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _annualised_build(count: int, i: Inputs) -> float:
    return count * i.hours_per_integration * i.loaded_hourly_rate / i.integration_lifetime_years


def _token_cost_per_year(i: Inputs) -> float:
    """Annual cost of the extra context MCP puts in front of the model, per request."""
    effective_rate = (
        i.input_cost_per_mtok * (1 - i.cache_hit_fraction)
        + i.input_cost_per_mtok * i.cache_read_multiplier * i.cache_hit_fraction
    )
    per_request = i.extra_tokens_per_request / 1_000_000 * effective_rate
    return per_request * i.requests_per_month * 12


def compute(i: Inputs) -> Outcome:
    n_direct = i.agents * i.systems
    n_mcp = i.agents + i.systems

    direct_build = _annualised_build(n_direct, i)
    mcp_build = _annualised_build(n_mcp, i)

    direct_maint = n_direct * i.maintenance_hours_per_integration_per_year * i.loaded_hourly_rate
    mcp_maint = n_mcp * i.maintenance_hours_per_integration_per_year * i.loaded_hourly_rate

    # A policy change (audit logging, PII redaction, a new allowlist) touches every
    # bespoke integration in the direct architecture, and one place with a gateway.
    direct_policy = (
        i.policy_changes_per_year * n_direct * i.hours_per_policy_change_direct * i.loaded_hourly_rate
    )
    mcp_policy = i.policy_changes_per_year * i.hours_per_policy_change_mcp * i.loaded_hourly_rate

    mcp_tokens = _token_cost_per_year(i)

    direct_total = direct_build + direct_maint + direct_policy
    mcp_total = mcp_build + mcp_maint + mcp_policy + mcp_tokens + i.mcp_gateway_cost_per_year

    # Crossover: the request volume at which MCP's token premium eats the integration
    # saving. None when MCP is cheaper (or dearer) regardless of volume.
    fixed_saving = (direct_build + direct_maint + direct_policy) - (
        mcp_build + mcp_maint + mcp_policy + i.mcp_gateway_cost_per_year
    )
    effective_rate = (
        i.input_cost_per_mtok * (1 - i.cache_hit_fraction)
        + i.input_cost_per_mtok * i.cache_read_multiplier * i.cache_hit_fraction
    )
    per_request = i.extra_tokens_per_request / 1_000_000 * effective_rate
    crossover = None
    if fixed_saving > 0 and per_request > 0:
        crossover = fixed_saving / (per_request * 12)

    return Outcome(
        direct_build_usd=direct_build,
        direct_maintain_usd=direct_maint,
        direct_policy_usd=direct_policy,
        direct_token_usd=0.0,
        direct_total_usd=direct_total,
        mcp_build_usd=mcp_build,
        mcp_maintain_usd=mcp_maint,
        mcp_policy_usd=mcp_policy,
        mcp_token_usd=mcp_tokens,
        mcp_infra_usd=i.mcp_gateway_cost_per_year,
        mcp_total_usd=mcp_total,
        integrations_direct=n_direct,
        integrations_mcp=n_mcp,
        annual_delta_usd=direct_total - mcp_total,
        crossover_requests_per_month=crossover,
    )


def extra_tokens_from_results(path: Path) -> int | None:
    """Read the measured schema delta (mcp minus direct) out of an agentic result file.

    Uses a measured number instead of a guessed one, which is the whole point of pairing
    this model with the benchmark.
    """
    blocks: dict[str, int] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("record") != "trial":
                continue
            arm = row["arm"]
            blocks[arm] = max(blocks.get(arm, 0), int(row.get("tool_block_tokens", 0)))
    if "direct" not in blocks:
        return None
    mcp_arms = [v for k, v in blocks.items() if k.startswith("mcp")]
    if not mcp_arms:
        return None
    return max(0, max(mcp_arms) - blocks["direct"])


def render(i: Inputs, o: Outcome) -> str:
    lines = [
        "N agents x M systems crossover",
        "=" * 62,
        f"  {i.agents} agents x {i.systems} systems, {i.requests_per_month:,} requests/month",
        f"  extra context per request under MCP: {i.extra_tokens_per_request:,} tokens "
        f"(cache hit fraction {i.cache_hit_fraction:.0%})",
        "",
        f"  Integrations to build and own:  direct {o.integrations_direct}"
        f"   vs   mcp {o.integrations_mcp}",
        "",
        "  Annual cost                              direct            mcp",
        "  " + "-" * 58,
        f"  build (amortised over {i.integration_lifetime_years:g}y)      "
        f"${o.direct_build_usd:>12,.0f}  ${o.mcp_build_usd:>12,.0f}",
        f"  maintenance                        ${o.direct_maintain_usd:>12,.0f}  "
        f"${o.mcp_maintain_usd:>12,.0f}",
        f"  policy changes                     ${o.direct_policy_usd:>12,.0f}  "
        f"${o.mcp_policy_usd:>12,.0f}",
        f"  extra tokens                       ${o.direct_token_usd:>12,.0f}  "
        f"${o.mcp_token_usd:>12,.0f}",
        f"  gateway infrastructure             ${0:>12,.0f}  ${o.mcp_infra_usd:>12,.0f}",
        "  " + "-" * 58,
        f"  total                              ${o.direct_total_usd:>12,.0f}  "
        f"${o.mcp_total_usd:>12,.0f}",
        "",
    ]
    if o.annual_delta_usd > 0:
        lines.append(f"  MCP is cheaper by ${o.annual_delta_usd:,.0f}/year at this volume.")
    else:
        lines.append(f"  Direct is cheaper by ${-o.annual_delta_usd:,.0f}/year at this volume.")

    if o.crossover_requests_per_month:
        lines.append(
            f"  Crossover at ~{o.crossover_requests_per_month:,.0f} requests/month: "
            "above that, MCP's token premium exceeds its integration saving."
        )
    else:
        lines.append(
            "  No crossover: at these integration counts the direct architecture is "
            "cheaper at every volume. That usually means A x S is small."
        )

    lines += [
        "",
        "  Sensitivity worth checking before you commit:",
        "    --cache-hit-fraction 0.9    what caching the tool block does to the token term",
        "    --agents / --systems        the term that actually decides it",
        "    --hours-per-policy-change-direct   governance, the cost nobody budgets",
        "",
        "  Every input is a guess until you measure it. Feed the token delta in with",
        "  --from-results so at least that one is not.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="MCP vs direct integration economics")
    defaults = Inputs()
    for field_name, value in asdict(defaults).items():
        flag = "--" + field_name.replace("_", "-")
        p.add_argument(flag, type=type(value), default=value)
    p.add_argument("--from-results", type=Path, default=None,
                   help="agentic JSONL to read the measured schema token delta from")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = p.parse_args(argv)

    kwargs = {k: getattr(args, k) for k in asdict(defaults)}
    inputs = Inputs(**kwargs)

    if args.from_results:
        measured = extra_tokens_from_results(args.from_results)
        if measured is not None:
            inputs.extra_tokens_per_request = measured
            print(f"[using measured schema delta from {args.from_results.name}: "
                  f"{measured} tokens]\n")
        else:
            print(f"[could not read a schema delta from {args.from_results.name}; "
                  "using the default]\n")

    outcome = compute(inputs)
    if args.json:
        print(json.dumps({"inputs": asdict(inputs), "outcome": outcome.as_dict()}, indent=2))
    else:
        print(render(inputs, outcome))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
