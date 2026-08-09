"""Render RESULTS.md from the newest result files.

The report is written to be readable by someone who did not run it, which means it states
what each number means, refuses to claim a difference smaller than its own confidence
interval, and surfaces the two findings people miss: schema utilisation and the minimum
cacheable prefix.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from ..config import SETTINGS
from .metrics import latest, load_jsonl, wilson


def _fmt(x: float, places: int = 2) -> str:
    return f"{x:.{places}f}"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def micro_section(rows: list[dict[str, Any]]) -> str:
    trials = [r for r in rows if r.get("record") == "micro"]
    if not trials:
        return ""
    params = next((r for r in rows if r.get("record") == "params"), {})

    reuse = params.get("session_reuse", True)
    mode = "pooled session" if reuse else "NAIVE: re-handshake per call"

    out = ["## Protocol overhead (no model in the loop)", ""]
    out.append(
        f"{params.get('iterations', '?')} iterations per arm after "
        f"{params.get('warmup', '?')} warmup calls, probing `{params.get('probe_tool', '?')}`, "
        f"**{mode}**. Upstream is the backend's self-reported service time and is the "
        "control term; overhead is wall clock minus upstream."
    )
    out.append("")
    if not reuse:
        # Without this the reader concludes MCP costs a second per call, which is a
        # statement about the client, not the protocol.
        out.append(
            "> **These numbers are the naive-client path.** The session is torn down and "
            "re-established for every single call, so each row includes a full connect, "
            "initialize and tools/list. This is not what MCP costs; it is what an "
            "unpooled client costs. Compare against a pooled run before drawing any "
            "conclusion about the protocol."
        )
        out.append("")
    out.append(
        "| Arm | Transport | Session | Overhead p50 | p95 | p99 | 95% CI on median | Total p50 | Upstream p50 |"
    )
    out.append("|---|---|---|---:|---:|---:|---|---:|---:|")
    for r in trials:
        ov, tot, up = r["overhead"], r["total"], r["upstream"]
        out.append(
            f"| `{r['arm']}` | {r['transport']} | {'pooled' if r.get('session_reuse', True) else 'per-call'} | "
            f"{_fmt(ov['p50'])} ms | {_fmt(ov['p95'])} ms | "
            f"{_fmt(ov['p99'])} ms | {_fmt(ov['ci_low'])} to {_fmt(ov['ci_high'])} ms | "
            f"{_fmt(tot['p50'])} ms | {_fmt(up['p50'])} ms |"
        )
    out.append("")

    base = next((r for r in trials if r["arm"] == "direct"), None)
    if base is None:
        out.append(
            "_No `direct` arm in this run, so there is no baseline to compare against. "
            "Re-run including `direct` for the relative numbers._"
        )
        out.append("")
    if base:
        b = base["overhead"]["p50"]
        out.append("**Overhead relative to the direct baseline (p50):**")
        out.append("")
        for r in trials:
            if r["arm"] == "direct":
                continue
            delta = r["overhead"]["p50"] - b
            out.append(f"- `{r['arm']}`: +{_fmt(delta)} ms per call")
        out.append("")
        out.append(
            "> Put these next to a model turn, which is typically 800 to 5000 ms. "
            "If the added milliseconds here are single-digit, the latency objection to "
            "MCP is real but not decision-relevant, and the argument has to be made on "
            "tokens instead."
        )
        out.append("")

    out.append("### Session handshake")
    out.append("")
    out.append("| Arm | Connect | Initialize | tools/list | Total | Per call at 1 | at 10 | at 100 | at 1000 |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in trials:
        a = r.get("amortised_session_ms", {})
        out.append(
            f"| `{r['arm']}` | {_fmt(r['session_connect_ms'])} ms | "
            f"{_fmt(r['session_initialize_ms'])} ms | {_fmt(r['session_list_tools_ms'])} ms | "
            f"{_fmt(r['session_total_ms'])} ms | {_fmt(a.get('per_call_at_1', 0))} | "
            f"{_fmt(a.get('per_call_at_10', 0))} | {_fmt(a.get('per_call_at_100', 0))} | "
            f"{_fmt(a.get('per_call_at_1000', 0))} |"
        )
    out.append("")
    out.append(
        "> The handshake is the number most sensitive to client configuration. A pooled "
        "client pays it once; a naive one pays it per request. Run with "
        "`--no-session-reuse` to see the naive path."
    )
    out.append("")
    return "\n".join(out)


def agentic_section(rows: list[dict[str, Any]]) -> str:
    trials = [r for r in rows if r.get("record") == "trial"]
    if not trials:
        return ""
    params = next((r for r in rows if r.get("record") == "params"), {})

    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trials:
        by_arm[t["arm"]].append(t)

    out = ["## Task suite (model in the loop)", ""]
    if params.get("provider") == "oracle":
        # Without this a reader sees 100% success and concludes the arms are equivalent.
        out.append(
            "> **This run used the `oracle` provider, which is a scripted perfect agent, "
            "not a model.** It solves every task by construction, so the success column "
            "is meaningless here and the wrong-tool column is structurally zero. What "
            "this run does validate is the plumbing: tool dispatch, state reset, grading "
            "and token accounting. The context-economics table below is still real, "
            "because schema weight does not depend on who is driving. Re-run with a real "
            "provider before drawing any conclusion about tool selection."
        )
        out.append("")
    # phrasing and distractor_kind are the two variables the headline result turns on.
    # A report that omits them is not interpretable: the same arm scores 100% or 60%
    # depending purely on these, and a reader cannot tell which run they are looking at.
    phrasing = params.get("phrasing", "explicit")
    kind = params.get("distractor_kind", "cross_domain")
    count = params.get("distractor_count", 0)
    out.append(
        f"Provider `{params.get('provider')}`, model `{params.get('model')}`, "
        f"{params.get('repeats')} repeats over {len(params.get('tasks', []))} tasks "
        f"(n={len(trials) // max(1, len(by_arm))} per arm), turn cap "
        f"{params.get('max_turns')}."
    )
    out.append("")
    out.append(
        f"**Conditions: `{phrasing}` request phrasing, {count} `{kind}` distractor tools.** "
        + (
            "This is the realistic condition: ambiguous requests plus semantically "
            "overlapping tools. Both are required to reproduce tool-selection failure."
            if phrasing == "vague" and kind == "near_miss" and count
            else "Note that explicit phrasing lets a model route on keyword overlap alone, "
            "so it will not surface tool-selection failure however many tools are loaded."
            if phrasing == "explicit"
            else ""
        )
    )
    out.append("")
    out.append(
        "| Arm | Success (Wilson 95%) | Turns | Tool calls | Wrong-tool | Tokens/task | $/task | Wall |"
    )
    out.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for arm, group in by_arm.items():
        n = len(group)
        wins = sum(1 for g in group if g["passed"])
        p, lo, hi = wilson(wins, n)
        tokens = mean(
            (g["usage"].get("input_tokens", 0)
             + g["usage"].get("cache_read_tokens", 0)
             + g["usage"].get("cache_write_tokens", 0)
             + g["usage"].get("output_tokens", 0))
            for g in group
        )
        calls = sum(g["tool_calls"] for g in group)
        wrong = sum(g["wrong_tool_calls"] for g in group)
        out.append(
            f"| `{arm}` | {_pct(p)} ({_pct(lo)} to {_pct(hi)}) | "
            f"{_fmt(mean(g['turns'] for g in group), 1)} | "
            f"{_fmt(mean(g['tool_calls'] for g in group), 1)} | "
            f"{_pct(wrong / calls) if calls else 'n/a'} | "
            f"{int(tokens)} | ${_fmt(mean(g['cost_usd'] for g in group), 4)} | "
            f"{_fmt(mean(g['wall_ms'] for g in group) / 1000, 1)} s |"
        )
    out.append("")

    widths = []
    for arm, group in by_arm.items():
        _, lo, hi = wilson(sum(1 for g in group if g["passed"]), len(group))
        widths.append(hi - lo)
    if widths:
        out.append(
            f"> The success intervals above are roughly {_pct(max(widths))} wide. "
            "Any difference between arms smaller than that is noise at this sample size, "
            "not a finding. Increase `--repeats` before drawing a conclusion from it."
        )
        out.append("")

    out.append("### Outcome breakdown")
    out.append("")
    out.append("| Arm | ok | graded_fail | turn_cap | provider_error |")
    out.append("|---|---:|---:|---:|---:|")
    for arm, group in by_arm.items():
        counts: dict[str, int] = defaultdict(int)
        for g in group:
            counts[g["outcome"]] += 1
        out.append(
            f"| `{arm}` | {counts['ok']} | {counts['graded_fail']} | "
            f"{counts['turn_cap']} | {counts['provider_error']} |"
        )
    out.append("")
    out.append(
        "> `turn_cap` and `graded_fail` are different failures. Running out of turns "
        "points at loop budget or tool chattiness; a graded failure points at reasoning "
        "or tool selection."
    )
    out.append("")

    out.append("### Context economics")
    out.append("")
    out.append("| Arm | Tools loaded | Tool block tokens | Schema utilisation | Wasted schema tokens |")
    out.append("|---|---:|---:|---:|---:|")
    for arm, group in by_arm.items():
        block = max(g["tool_block_tokens"] for g in group)
        util = mean(g["schema_utilisation"] for g in group)
        out.append(
            f"| `{arm}` | {max(g['tools_loaded'] for g in group)} | {block} | "
            f"{_pct(util)} | {int(block * (1 - util))} |"
        )
    out.append("")
    out.append(
        "> Schema utilisation is the share of loaded tool-schema weight belonging to "
        "tools the agent actually called. The complement is context you pay for on every "
        "turn and never use. This is the number that makes gateway bloat legible to "
        "someone who reads a cost report."
    )
    out.append("")

    notes = {g.get("cacheability_note") for g in trials if g.get("cacheability_note")}
    if notes:
        out.append("### Cacheability warnings")
        out.append("")
        for note in sorted(notes):
            out.append(f"- {note}")
        out.append("")
        out.append(
            "> Worth sitting with: a tight hand-written tool block can be **too small to "
            "cache at all**, while a bloated gateway block is the only one above the "
            "minimum cacheable prefix. The intuition that smaller is always cheaper does "
            "not survive contact with caching."
        )
        out.append("")
    return "\n".join(out)


def build(
    results_dir: Path | None = None,
    micro_file: Path | None = None,
    agentic_file: Path | None = None,
) -> str:
    """Render the report.

    Defaults to the newest run in `results/`, which is right while iterating and wrong
    for publication: the newest run is usually whatever you last poked at, not the run
    that backs your headline. Pass explicit files (or point `results_dir` at
    `results-reference/`) when generating the committed RESULTS.md.
    """
    d = results_dir or SETTINGS.results_dir
    parts = [
        "# Results",
        "",
        "Generated by `python -m bench.cli report`. Read `METHODOLOGY.md` before quoting "
        "any number from this file: it documents what is controlled, what is not, and "
        "which of these results will not generalise to your stack.",
        "",
    ]

    micro_path = micro_file or latest(d, "micro")
    if micro_path:
        parts.append(f"_Microbench source: `{micro_path.name}`_")
        parts.append("")
        parts.append(micro_section(load_jsonl(micro_path)))
    else:
        parts.append("_No microbench results yet. Run `make micro`._\n")

    agentic_path = agentic_file or latest(d, "agentic")
    if agentic_path:
        parts.append(f"_Agentic source: `{agentic_path.name}`_")
        parts.append("")
        parts.append(agentic_section(load_jsonl(agentic_path)))
    else:
        parts.append("_No agentic results yet. Run `make agentic`._\n")

    parts.append("## What this does not tell you")
    parts.append("")
    parts.append(
        "- Whether MCP is worth it **for you**. That depends on how many agents times how "
        "many systems you run, which is what `analysis/crossover.py` prices."
    )
    parts.append(
        "- How your model behaves. Tool-selection results are model-specific and do not "
        "transfer across model families."
    )
    parts.append(
        "- What your network costs. Substitute a measured RTT into the netem sweep rather "
        "than trusting the lab default."
    )
    parts.append("")
    return "\n".join(parts)


def write(
    path: Path | None = None,
    results_dir: Path | None = None,
    micro_file: Path | None = None,
    agentic_file: Path | None = None,
) -> Path:
    target = path or (SETTINGS.results_dir.parent / "RESULTS.md")
    target.write_text(
        build(results_dir=results_dir, micro_file=micro_file, agentic_file=agentic_file),
        encoding="utf-8",
    )
    return target
