"""Exact per-configuration detail for one cell of the phrasing/distractor grid.

The summary tools report rates. When prose makes a claim about a specific cell ("produces
zero wrong tool calls") you need the exact counter and, when it is non-zero, the tool names
behind it. A rate cannot tell you whether three off-task calls were distractors or
legitimate ops tools the task's relevant-set simply did not list, and those mean opposite
things.

    python -m analysis.cell_detail --phrasing vague --kind near_miss --distractors 0
    python -m analysis.cell_detail --arm mcp_filtered
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
from pathlib import Path

from bench.workload.distractors import is_distractor
from bench.workload.tasks import TASKS_BY_ID


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="exact detail for one configuration")
    p.add_argument("--results", type=Path, default=Path("results-reference"))
    p.add_argument("--provider", default="deepseek")
    p.add_argument("--arm", default=None)
    p.add_argument("--phrasing", default=None)
    p.add_argument("--kind", default=None)
    p.add_argument("--distractors", type=int, default=None)
    args = p.parse_args(argv)

    agg: dict[tuple, dict] = collections.defaultdict(
        lambda: {
            "n": 0, "pass": 0, "tool_calls": 0, "wrong": 0,
            "block": 0, "offtask": collections.Counter(), "distractor_calls": 0,
        }
    )

    for f in sorted(glob.glob(str(args.results / "agentic-*.jsonl"))):
        rows = [json.loads(l) for l in Path(f).open(encoding="utf-8") if l.strip()]
        params = next((r for r in rows if r.get("record") == "params"), {})
        if params.get("provider") != args.provider:
            continue
        ph = params.get("phrasing", "explicit")
        kd = params.get("distractor_kind", "cross_domain")
        dc = params.get("distractor_count", 0)
        if args.phrasing and ph != args.phrasing:
            continue
        if args.kind and kd != args.kind:
            continue
        if args.distractors is not None and dc != args.distractors:
            continue

        for t in rows:
            if t.get("record") != "trial":
                continue
            if args.arm and t["arm"] != args.arm:
                continue
            key = (t["arm"], ph, kd, dc)
            a = agg[key]
            a["n"] += 1
            a["pass"] += 1 if t["passed"] else 0
            a["tool_calls"] += t.get("tool_calls", 0)
            a["wrong"] += t.get("wrong_tool_calls", 0)
            a["block"] = max(a["block"], t.get("tool_block_tokens", 0))
            relevant = set(TASKS_BY_ID[t["task"]].relevant_tools)
            for name in t.get("called_tools", []):
                if name not in relevant:
                    a["offtask"][name] += 1
                    if is_distractor(name):
                        a["distractor_calls"] += 1

    if not agg:
        print("no matching configuration")
        return 1

    for (arm, ph, kd, dc), a in sorted(agg.items()):
        print(f"\n{arm}  phrasing={ph}  kind={kd}  distractors={dc}")
        print(f"  n={a['n']}  pass={a['pass']}/{a['n']} ({a['pass']/a['n']*100:.0f}%)")
        print(f"  tool_calls={a['tool_calls']} (exact)   wrong_tool_calls={a['wrong']} (exact)")
        print(f"  tool_block_tokens={a['block']}")
        print(f"  distinct distractor tools touched={a['distractor_calls']} (deduped, lower bound)")
        if a["offtask"]:
            named = ", ".join(f"{k} x{v}" for k, v in a["offtask"].most_common(8))
            print(f"  off-task tool names: {named}")
        else:
            print("  off-task tool names: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
