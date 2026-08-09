"""Inspect a distractor sweep across result files.

The sweep command prints a summary line per count, but the interesting question when the
success rate does NOT move is *why*: which tools were actually called outside the task's
relevant set, and whether any of them were distractors. A wrong-tool rate made entirely of
legitimate-but-unnecessary ops calls means something very different from one made of HR and
finance tools the agent should never have touched.

    python -m analysis.sweep_report              # newest 6 agentic files
    python -m analysis.sweep_report --last 10
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
from pathlib import Path

from bench.workload.distractors import is_distractor
from bench.workload.tasks import TASKS_BY_ID

HEADER = (
    f"{'distr':>6} {'arm':<14} {'pass':>8} {'calls':>6} {'off-task':>10} "
    f"{'distr':>6} {'util':>7} {'tool_tok':>9} {'cache-hit':>10} {'$/task':>9}"
)


def load(path: Path) -> tuple[dict, list[dict]]:
    rows = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    params = next((r for r in rows if r.get("record") == "params"), {})
    trials = [r for r in rows if r.get("record") == "trial"]
    return params, trials


def emit(params: dict, arm: str, trials: list[dict]) -> None:
    offenders: collections.Counter[str] = collections.Counter()
    total_calls = off_task = distractor_calls = 0
    for t in trials:
        relevant = set(TASKS_BY_ID[t["task"]].relevant_tools)
        for name in t["called_tools"]:
            total_calls += 1
            if name not in relevant:
                off_task += 1
                offenders[name] += 1
                if is_distractor(name):
                    distractor_calls += 1

    n = len(trials)
    passed = sum(1 for t in trials if t["passed"])
    util = sum(t["schema_utilisation"] for t in trials) / n
    cost = sum(t["cost_usd"] for t in trials) / n
    block = max(t["tool_block_tokens"] for t in trials)

    # Share of input tokens served from cache. On a provider with an automatic prefix
    # cache this is the whole cache story: you do not place a breakpoint, you only get to
    # observe whether it happened to hit.
    cached = sum(t["usage"].get("cache_read_tokens", 0) for t in trials)
    total_in = sum(
        t["usage"].get("input_tokens", 0)
        + t["usage"].get("cache_read_tokens", 0)
        + t["usage"].get("cache_write_tokens", 0)
        for t in trials
    )
    hit = cached / total_in if total_in else 0.0

    print(
        f"{params.get('distractor_count', 0):>6} {arm:<14} "
        f"{passed:>4}/{n:<3} {total_calls:>6} "
        f"{off_task:>4} ({off_task / max(1, total_calls) * 100:4.1f}%) "
        f"{distractor_calls:>6} {util * 100:>6.1f}% {block:>9} "
        f"{hit * 100:>9.1f}% {cost:>9.5f}"
    )
    if offenders:
        top = ", ".join(f"{k} x{v}" for k, v in offenders.most_common(5))
        print(f"       off-task tools: {top}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="distractor sweep breakdown")
    p.add_argument("--last", type=int, default=6)
    p.add_argument("--results", type=Path, default=Path("results"))
    args = p.parse_args(argv)

    files = sorted(glob.glob(str(args.results / "agentic-*.jsonl")))[-args.last:]
    print(HEADER)
    print("-" * len(HEADER))

    for f in files:
        params, all_trials = load(Path(f))
        if not all_trials:
            continue
        # A file can hold several arms. Grouping matters: printing one arm's label over
        # another arm's token counts is exactly the quiet mislabelling that makes a
        # benchmark untrustworthy.
        by_arm: dict[str, list[dict]] = collections.defaultdict(list)
        for t in all_trials:
            by_arm[t["arm"]].append(t)
        for arm in sorted(by_arm):
            emit(params, arm, by_arm[arm])

    print()
    print("'off-task' counts any call outside the task's declared relevant set, which")
    print("includes legitimate ops tools the agent did not strictly need. 'distr' counts")
    print("only calls into the injected noise tools. If off-task is non-zero but distr is")
    print("zero, tool bloat cost you tokens and not correctness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
