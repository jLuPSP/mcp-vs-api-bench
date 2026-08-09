"""Microbenchmark: protocol overhead with no model in the loop.

This is where the latency claim gets settled. Thousands of identical tool calls per arm,
warmup discarded, and every call decomposed into upstream service time (the control) and
protocol overhead (the thing under test). LLM variance would swamp a 3 ms difference, so
the model is deliberately absent.

Session setup is measured separately and reported both as a one-time cost and amortised
across several request volumes, because whether the handshake matters at all depends
entirely on whether the client pools connections.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..config import SETTINGS, build_arm
from .metrics import LatencyStats, ResultWriter, latency_stats

#: A cheap, read-only call with a small stable response. Deliberately not a query whose
#: payload size varies, which would confound serialization cost with response size.
PROBE_TOOL = "get_oncall"
PROBE_ARGS: dict[str, Any] = {}


@dataclass
class MicroResult:
    record: str = "micro"
    arm: str = ""
    transport: str = ""
    session_reuse: bool = True
    iterations: int = 0
    errors: int = 0
    total: dict[str, Any] = field(default_factory=dict)
    upstream: dict[str, Any] = field(default_factory=dict)
    overhead: dict[str, Any] = field(default_factory=dict)
    session_connect_ms: float = 0.0
    session_initialize_ms: float = 0.0
    session_list_tools_ms: float = 0.0
    session_total_ms: float = 0.0
    amortised_session_ms: dict[str, float] = field(default_factory=dict)
    tool_count: int = 0


async def run_arm(
    arm_name: str,
    iterations: int = 2000,
    warmup: int = 100,
    session_reuse: bool = True,
) -> MicroResult:
    arm = build_arm(arm_name, session_reuse=session_reuse)
    totals: list[float] = []
    upstreams: list[float] = []
    overheads: list[float] = []
    errors = 0

    async with arm:
        tools = await arm.list_tools()

        for i in range(iterations + warmup):
            t0 = time.perf_counter()
            result = await arm.call_tool(PROBE_TOOL, dict(PROBE_ARGS))
            total_ms = (time.perf_counter() - t0) * 1000.0
            if result.is_error:
                errors += 1
                continue
            if i < warmup:
                continue
            totals.append(total_ms)
            upstreams.append(result.upstream_ms)
            # Can go slightly negative if the server's own clock overlaps the client's
            # measurement window. Floor at zero rather than pretending it is meaningful.
            overheads.append(max(0.0, total_ms - result.upstream_ms))

        session = arm.session_cost

    res = MicroResult(
        arm=arm_name,
        transport=arm.transport,
        session_reuse=session_reuse,
        iterations=len(totals),
        errors=errors,
        total=latency_stats(totals).as_dict(),
        upstream=latency_stats(upstreams).as_dict(),
        overhead=latency_stats(overheads).as_dict(),
        session_connect_ms=session.connect_ms,
        session_initialize_ms=session.initialize_ms,
        session_list_tools_ms=session.list_tools_ms,
        session_total_ms=session.total_ms,
        tool_count=len(tools),
    )
    # Amortising the handshake is the honest way to present it: a 40 ms setup is fatal at
    # one call per session and irrelevant at a thousand.
    res.amortised_session_ms = {
        f"per_call_at_{n}": (session.total_ms / n if n else 0.0) for n in (1, 10, 100, 1000)
    }
    return res


async def run(
    arms: list[str],
    iterations: int = 2000,
    warmup: int = 100,
    session_reuse: bool = True,
) -> list[MicroResult]:
    results: list[MicroResult] = []
    with ResultWriter(SETTINGS.results_dir, "micro", SETTINGS.fingerprint()) as writer:
        writer.write(
            {
                "record": "params",
                "iterations": iterations,
                "warmup": warmup,
                "session_reuse": session_reuse,
                "probe_tool": PROBE_TOOL,
            }
        )
        for arm_name in arms:
            res = await run_arm(arm_name, iterations, warmup, session_reuse)
            writer.write(res)
            results.append(res)
    return results
