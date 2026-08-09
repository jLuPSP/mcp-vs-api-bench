"""Command line entry point.

    python -m bench.cli micro   --arms direct,mcp_stdio,mcp_sidecar,mcp_remote
    python -m bench.cli agentic --provider deepseek --repeats 5
    python -m bench.cli sweep-distractors --arm mcp_remote --counts 0,10,30,60,150
    python -m bench.cli cache-experiment --provider anthropic
    python -m bench.cli validate
    python -m bench.cli report
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import warnings
from pathlib import Path

from rich.console import Console

# Client-side chatter would otherwise interleave with results and, worse, cost time
# inside the measurement window.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)
# Cosmetic third-party noise from a transitive dependency of the MCP SDK.
warnings.filterwarnings("ignore", module=r"pydantic_settings.*")
warnings.filterwarnings("ignore", message=r".*incomplete definition.*")

from .config import ARM_NAMES, SETTINGS
from .harness import agentic, micro, report
from .providers import PROVIDER_NAMES
from .workload.tasks import TASKS, validate_suite

console = Console()


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _ints(value: str) -> list[int]:
    return [int(v.strip()) for v in value.split(",") if v.strip()]


# --- commands ----------------------------------------------------------------


async def cmd_micro(args: argparse.Namespace) -> int:
    arms = _csv(args.arms)
    console.print(f"[bold]Microbench[/bold] arms={arms} iterations={args.iterations} "
                  f"session_reuse={not args.no_session_reuse}")
    results = await micro.run(
        arms,
        iterations=args.iterations,
        warmup=args.warmup,
        session_reuse=not args.no_session_reuse,
    )
    for r in results:
        console.print(
            f"  {r.arm:<14} overhead p50={r.overhead['p50']:.2f} ms  "
            f"p95={r.overhead['p95']:.2f} ms  session={r.session_total_ms:.1f} ms  "
            f"errors={r.errors}"
        )
    return 0


async def cmd_agentic(args: argparse.Namespace) -> int:
    arms = _csv(args.arms)
    tasks = _csv(args.tasks) if args.tasks else None
    console.print(
        f"[bold]Agentic[/bold] provider={args.provider} arms={arms} repeats={args.repeats} "
        f"distractors={args.distractors} cache_tools={args.cache_tools}"
    )
    results = await agentic.run(
        arms,
        args.provider,
        tasks=tasks,
        repeats=args.repeats,
        max_turns=args.max_turns,
        cache_tools=args.cache_tools,
        distractor_count=args.distractors,
        distractor_kind=args.kind,
        phrasing=args.phrasing,
        session_reuse=not args.no_session_reuse,
        model=args.model,
    )
    for arm in arms:
        group = [r for r in results if r.arm == arm]
        if not group:
            continue
        wins = sum(1 for r in group if r.passed)
        console.print(f"  {arm:<14} {wins}/{len(group)} passed")
    return 0


async def cmd_sweep(args: argparse.Namespace) -> int:
    """Distractor sweep: the direct answer to 'can we point agents at the gateway?'"""
    counts = _ints(args.counts)
    console.print(
        f"[bold]Distractor sweep[/bold] arm={args.arm} counts={counts} kind={args.kind}"
    )
    for count in counts:
        results = await agentic.run(
            [args.arm],
            args.provider,
            repeats=args.repeats,
            max_turns=args.max_turns,
            distractor_count=count,
            distractor_kind=args.kind,
            phrasing=args.phrasing,
            model=args.model,
        )
        wins = sum(1 for r in results if r.passed)
        calls = sum(r.tool_calls for r in results)
        wrong = sum(r.wrong_tool_calls for r in results)
        util = sum(r.schema_utilisation for r in results) / max(1, len(results))
        block = max((r.tool_block_tokens for r in results), default=0)
        console.print(
            f"  +{count:<4} distractors  success={wins}/{len(results)}  "
            f"wrong_tool={(wrong / calls * 100 if calls else 0):.1f}%  "
            f"schema_util={util * 100:.1f}%  tool_tokens={block}"
        )
    return 0


async def cmd_cache(args: argparse.Namespace) -> int:
    """Same workload with and without a cache breakpoint on the tool block."""
    console.print(f"[bold]Cache experiment[/bold] provider={args.provider} arm={args.arm}")
    for cache_tools in (False, True):
        results = await agentic.run(
            [args.arm],
            args.provider,
            repeats=args.repeats,
            max_turns=args.max_turns,
            cache_tools=cache_tools,
            distractor_count=args.distractors,
            model=args.model,
        )
        cost = sum(r.cost_usd for r in results) / max(1, len(results))
        reads = sum(r.usage.get("cache_read_tokens", 0) for r in results)
        writes = sum(r.usage.get("cache_write_tokens", 0) for r in results)
        note = next((r.cacheability_note for r in results if r.cacheability_note), None)
        console.print(
            f"  cache_tools={str(cache_tools):<5}  $/task={cost:.5f}  "
            f"cache_read={reads}  cache_write={writes}"
        )
        if note:
            console.print(f"    [yellow]warning[/yellow]: {note}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Check the seed produces a non-degenerate instance of every task."""
    problems = validate_suite()
    console.print(f"[bold]Suite[/bold] seed={SETTINGS.seed} tasks={len(TASKS)}")
    if problems:
        for p in problems:
            console.print(f"  [red]degenerate[/red] {p}")
        console.print(
            "\nA degenerate task scores zero for every arm, which looks like a finding "
            "and is not one. Change BENCH_SEED and re-validate."
        )
        return 1
    for t in TASKS:
        console.print(f"  [green]ok[/green] {t.id:<26} tools={len(t.relevant_tools)} tags={t.tags}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    path = report.write(
        results_dir=args.results_dir,
        micro_file=args.micro_file,
        agentic_file=args.agentic_file,
    )
    console.print(f"wrote {path}")
    return 0


def cmd_check_pricing(args: argparse.Namespace) -> int:
    pricing = SETTINGS.pricing()
    console.print(f"[bold]Pricing table[/bold] version={pricing.get('version')}")
    unverified = []
    for name, prov in pricing.get("providers", {}).items():
        mark = "[green]verified[/green]" if prov.get("verified") else "[yellow]UNVERIFIED[/yellow]"
        console.print(f"  {name:<12} {mark}  explicit_cache={prov['cache']['explicit_breakpoints']}")
        if not prov.get("verified"):
            unverified.append(name)
        for model, spec in prov.get("models", {}).items():
            console.print(
                f"    {model:<24} in=${spec['input_per_mtok']}/Mtok  "
                f"out=${spec['output_per_mtok']}/Mtok  "
                f"min_cacheable={spec.get('min_cacheable_tokens')}"
            )
    if unverified:
        console.print(
            f"\n[yellow]Verify before quoting dollars:[/yellow] {', '.join(unverified)}. "
            "Edit bench/pricing.yaml."
        )
    return 0


# --- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench", description="MCP vs direct API benchmark")
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("micro", help="protocol overhead, no model")
    m.add_argument("--arms", default=",".join(ARM_NAMES))
    m.add_argument("--iterations", type=int, default=2000)
    m.add_argument("--warmup", type=int, default=100)
    m.add_argument("--no-session-reuse", action="store_true",
                   help="re-handshake per call, reproducing a naive client")
    m.set_defaults(func=cmd_micro, is_async=True)

    a = sub.add_parser("agentic", help="graded task suite with a model")
    a.add_argument("--arms", default=",".join(ARM_NAMES))
    a.add_argument("--provider", default="ollama", choices=PROVIDER_NAMES)
    a.add_argument("--model", default=None)
    a.add_argument("--tasks", default=None, help="comma-separated task ids")
    a.add_argument("--repeats", type=int, default=5)
    a.add_argument("--max-turns", type=int, default=12)
    a.add_argument("--distractors", type=int, default=0)
    a.add_argument("--cache-tools", action="store_true")
    a.add_argument("--no-session-reuse", action="store_true")
    a.add_argument("--phrasing", default="explicit", choices=["explicit", "vague"])
    a.add_argument("--kind", default="cross_domain", choices=["cross_domain", "near_miss"])
    a.set_defaults(func=cmd_agentic, is_async=True)

    s = sub.add_parser("sweep-distractors", help="tool-count sweep")
    s.add_argument("--arm", default="mcp_remote")
    s.add_argument("--provider", default="ollama", choices=PROVIDER_NAMES)
    s.add_argument("--model", default=None)
    s.add_argument("--counts", default="0,10,30,60,150")
    s.add_argument("--repeats", type=int, default=3)
    s.add_argument("--max-turns", type=int, default=12)
    s.add_argument(
        "--kind",
        default="cross_domain",
        choices=["cross_domain", "near_miss"],
        help="cross_domain: tools from unrelated domains (easy). "
             "near_miss: same-domain tools with overlapping semantics (hard).",
    )
    s.add_argument(
        "--phrasing",
        default="explicit",
        choices=["explicit", "vague"],
        help="explicit prompts name their domain ('support ticket', 'P1'), so tools can "
             "be picked on keyword overlap. vague prompts remove the giveaway vocabulary "
             "and force real discrimination. Use vague with --kind near_miss for the "
             "hardest test.",
    )
    s.set_defaults(func=cmd_sweep, is_async=True)

    c = sub.add_parser("cache-experiment", help="tool-block caching on/off")
    c.add_argument("--arm", default="mcp_remote")
    c.add_argument("--provider", default="anthropic", choices=PROVIDER_NAMES)
    c.add_argument("--model", default=None)
    c.add_argument("--repeats", type=int, default=3)
    c.add_argument("--max-turns", type=int, default=12)
    c.add_argument("--distractors", type=int, default=0)
    c.set_defaults(func=cmd_cache, is_async=True)

    v = sub.add_parser("validate", help="check the seed yields solvable tasks")
    v.set_defaults(func=cmd_validate, is_async=False)

    r = sub.add_parser("report", help="render RESULTS.md")
    r.add_argument("--results-dir", type=Path, default=None,
                   help="directory to read from (default results/; use results-reference/ "
                        "to regenerate the committed report)")
    r.add_argument("--micro-file", type=Path, default=None,
                   help="specific micro JSONL, instead of the newest")
    r.add_argument("--agentic-file", type=Path, default=None,
                   help="specific agentic JSONL, instead of the newest. Use this when "
                        "publishing: the newest run is rarely the headline run.")
    r.set_defaults(func=cmd_report, is_async=False)

    cp = sub.add_parser("check-pricing", help="show the pricing table and flag unverified rows")
    cp.set_defaults(func=cmd_check_pricing, is_async=False)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "is_async", False):
            return asyncio.run(args.func(args))
        return args.func(args)
    except RuntimeError as exc:
        # Operator errors (unreachable endpoint, missing API key) are expected failure
        # modes for a benchmark harness, not bugs. Print the sentence, not the stack.
        console.print(f"[red]error:[/red] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
