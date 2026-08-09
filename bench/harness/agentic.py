"""Agentic runner: the graded task suite with a model in the loop.

Structure per trial: reset the backend, connect the arm, load tools (plus distractors if
sweeping), run the agent loop to a turn cap, snapshot backend state, grade
deterministically, record. Nothing about the loop varies by arm except the arm itself.

Two things here are load-bearing for honesty:

* Hitting the turn cap is recorded as its own outcome, not silently folded into failure.
  "Ran out of turns" and "confidently did the wrong thing" are different problems with
  different fixes.
* Distractor tools return plausible empty payloads rather than errors. An error would
  prompt the model to correct itself, which would understate the cost of tool bloat.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..arms.base import Arm, ToolResult, ToolSpec
from ..config import SETTINGS, build_arm
from ..providers import build_provider
from ..providers.base import Usage
from ..workload import distractors as dist
from ..workload.tasks import TASKS, TASKS_BY_ID, Task
from .metrics import ResultWriter, cacheability_warning, token_cost

SYSTEM_PROMPT = (
    "You are an operations assistant with access to tools. Use the tools to inspect and "
    "modify the operations systems as needed. Complete the whole task before replying. "
    "When you have finished, reply with a short summary and stop calling tools."
)


@dataclass
class TrialResult:
    record: str = "trial"
    arm: str = ""
    transport: str = ""
    provider: str = ""
    model: str = ""
    task: str = ""
    repeat: int = 0
    distractor_count: int = 0
    cache_tools: bool = False
    session_reuse: bool = True
    phrasing: str = "explicit"

    passed: bool = False
    detail: str = ""
    outcome: str = ""            # ok | turn_cap | provider_error | graded_fail

    turns: int = 0
    tool_calls: int = 0
    wrong_tool_calls: int = 0
    tool_errors: int = 0
    wall_ms: float = 0.0
    model_ms: float = 0.0
    tool_ms: float = 0.0
    upstream_ms: float = 0.0

    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0

    tools_loaded: int = 0
    tool_block_tokens: int = 0
    schema_utilisation: float = 0.0
    cacheability_note: str | None = None
    called_tools: list[str] = field(default_factory=list)
    error: str | None = None


async def reset_backend(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        await client.post("/_bench/reset")


async def fetch_state(base_url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        resp = await client.get("/_bench/state")
        resp.raise_for_status()
        return resp.json()


def _schema_weight(tool: ToolSpec) -> int:
    """Serialized size of one tool, used to allocate the measured block total."""
    return len(json.dumps(tool.to_anthropic(), sort_keys=True))


def schema_utilisation(all_tools: list[ToolSpec], called: set[str]) -> float:
    """Fraction of loaded schema weight belonging to tools the agent actually called.

    Length-allocated rather than individually token-counted: measuring every tool
    separately would cost one probe request per tool, which is prohibitive at 150
    distractors. The measured block total is exact; this is how it is apportioned.
    """
    total = sum(_schema_weight(t) for t in all_tools)
    if total == 0:
        return 0.0
    used = sum(_schema_weight(t) for t in all_tools if t.name in called)
    return used / total


async def run_trial(
    arm: Arm,
    provider: Any,
    task: Task,
    tools: list[ToolSpec],
    real_tool_names: set[str],
    *,
    repeat: int = 0,
    max_turns: int = 12,
    cache_tools: bool = False,
    distractor_count: int = 0,
    tool_block_tokens: int = 0,
    pricing: dict[str, Any] | None = None,
    phrasing: str = "explicit",
) -> TrialResult:
    res = TrialResult(
        arm=arm.name,
        transport=arm.transport,
        provider=provider.name,
        model=provider.model,
        task=task.id,
        repeat=repeat,
        distractor_count=distractor_count,
        cache_tools=cache_tools,
        session_reuse=getattr(arm, "session_reuse", True),
        tools_loaded=len(tools),
        tool_block_tokens=tool_block_tokens,
    )

    await reset_backend(SETTINGS.backend_url)

    res.phrasing = phrasing
    convo = provider.new_conversation(SYSTEM_PROMPT, task.text(phrasing))
    total_usage = Usage()
    called: list[str] = []
    final_text = ""
    wall0 = time.perf_counter()

    for turn_index in range(max_turns):
        turn = await provider.turn(convo, tools, cache_tools=cache_tools)
        res.turns = turn_index + 1
        res.model_ms += turn.latency_ms
        total_usage = total_usage + turn.usage

        if turn.error:
            res.outcome = "provider_error"
            res.error = turn.error
            break

        if not turn.tool_calls:
            final_text = turn.text
            res.outcome = "ok"
            break

        results: list[ToolResult] = []
        for call in turn.tool_calls:
            called.append(call.name)
            res.tool_calls += 1
            if call.name not in task.relevant_tools:
                res.wrong_tool_calls += 1

            t0 = time.perf_counter()
            if call.name in real_tool_names:
                out = await arm.call_tool(call.name, call.args)
            else:
                # A distractor, or a hallucinated name. A real gateway would route this
                # somewhere and return data, so return a plausible empty payload rather
                # than an error the model can correct against.
                out = ToolResult(content=json.dumps({"count": 0, "results": []}))
            res.tool_ms += (time.perf_counter() - t0) * 1000.0
            res.upstream_ms += out.upstream_ms
            if out.is_error:
                res.tool_errors += 1
            results.append(out)

        provider.add_tool_results(convo, turn, results)
    else:
        res.outcome = "turn_cap"

    res.wall_ms = (time.perf_counter() - wall0) * 1000.0
    res.usage = total_usage.as_dict()
    res.called_tools = sorted(set(called))
    res.schema_utilisation = schema_utilisation(tools, set(called))

    if pricing:
        res.cost_usd = token_cost(res.usage, provider.name, provider.model, pricing)
        res.cacheability_note = cacheability_warning(
            tool_block_tokens, provider.name, provider.model, pricing
        )

    # Grade on state regardless of how the loop ended: an agent that hit the turn cap may
    # still have completed the work.
    state = await fetch_state(SETTINGS.backend_url)
    verdict = task.check(state, final_text)
    res.passed = verdict.passed
    res.detail = verdict.detail
    if not res.passed and res.outcome == "ok":
        res.outcome = "graded_fail"

    return res


async def run(
    arms: list[str],
    provider_name: str,
    *,
    tasks: list[str] | None = None,
    repeats: int = 5,
    max_turns: int = 12,
    cache_tools: bool = False,
    distractor_count: int = 0,
    session_reuse: bool = True,
    model: str | None = None,
    distractor_kind: str = "cross_domain",
    phrasing: str = "explicit",
) -> list[TrialResult]:
    provider = build_provider(provider_name, model=model)
    pricing = SETTINGS.pricing()
    suite = [TASKS_BY_ID[t] for t in tasks] if tasks else TASKS
    # cross_domain asks "can the model tell ops from HR". near_miss asks "can it tell
    # list_tickets from list_service_requests", which is the discrimination a real shared
    # gateway actually demands.
    if distractor_kind == "near_miss":
        noise = dist.generate_near_miss(distractor_count, seed=SETTINGS.seed)
    else:
        noise = dist.generate(distractor_count, seed=SETTINGS.seed)
    results: list[TrialResult] = []

    with ResultWriter(SETTINGS.results_dir, "agentic", SETTINGS.fingerprint()) as writer:
        writer.write(
            {
                "record": "params",
                "arms": arms,
                "provider": provider_name,
                "model": provider.model,
                "tasks": [t.id for t in suite],
                "repeats": repeats,
                "max_turns": max_turns,
                "cache_tools": cache_tools,
                "distractor_count": distractor_count,
                "distractor_kind": distractor_kind,
                "session_reuse": session_reuse,
                "phrasing": phrasing,
            }
        )

        try:
            for arm_name in arms:
                for task in suite:
                    # mcp_filtered narrows to the task's relevant tools. Every other arm
                    # loads whatever its server advertises, which is the point.
                    tool_filter = task.relevant_tools if arm_name == "mcp_filtered" else None
                    arm = build_arm(arm_name, session_reuse=session_reuse, tool_filter=tool_filter)

                    async with arm:
                        real = await arm.list_tools()
                        real_names = {t.name for t in real}
                        tools = real + noise
                        # An arm that filters per task filters the WHOLE advertised
                        # surface, not just its own server's tools. Leaving injected
                        # noise in a "filtered" arm would model a filter that does not
                        # filter, and would understate what curation actually buys.
                        if getattr(arm, "tool_filter", None):
                            allowed = set(arm.tool_filter)
                            tools = [t for t in tools if t.name in allowed]

                        # Measure the tool block once per (arm, task, distractor count).
                        try:
                            block_tokens = await provider.count_tool_tokens(tools)
                        except Exception as exc:  # noqa: BLE001 - measurement is best-effort
                            block_tokens = 0
                            writer.write(
                                {
                                    "record": "warning",
                                    "where": "count_tool_tokens",
                                    "arm": arm_name,
                                    "error": f"{type(exc).__name__}: {exc}",
                                }
                            )

                        for repeat in range(repeats):
                            res = await run_trial(
                                arm,
                                provider,
                                task,
                                tools,
                                real_names,
                                repeat=repeat,
                                max_turns=max_turns,
                                cache_tools=cache_tools,
                                distractor_count=distractor_count,
                                tool_block_tokens=block_tokens,
                                pricing=pricing,
                                phrasing=phrasing,
                            )
                            writer.write(res)
                            results.append(res)
        finally:
            await provider.aclose()

    return results
