"""Total spend across result files, by provider and model.

Cheap to run and worth running before and after a big sweep. The per-task cost in the
report is an average; this is the bill.

    python -m analysis.spend
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="total benchmark spend")
    p.add_argument("--results", type=Path, default=Path("results"))
    args = p.parse_args(argv)

    totals: dict[tuple[str, str], dict[str, float]] = collections.defaultdict(
        lambda: {"usd": 0.0, "trials": 0, "in": 0, "cached": 0, "out": 0}
    )
    for f in sorted(glob.glob(str(args.results / "agentic-*.jsonl"))):
        for line in Path(f).open(encoding="utf-8"):
            row = json.loads(line)
            if row.get("record") != "trial":
                continue
            key = (row["provider"], row["model"])
            t = totals[key]
            t["usd"] += row.get("cost_usd", 0.0)
            t["trials"] += 1
            u = row.get("usage", {})
            t["in"] += u.get("input_tokens", 0)
            t["cached"] += u.get("cache_read_tokens", 0)
            t["out"] += u.get("output_tokens", 0)

    print(f"{'provider/model':<28} {'trials':>7} {'input':>10} {'cached':>10} {'output':>9} {'USD':>9}")
    print("-" * 78)
    grand = 0.0
    for (prov, model), t in sorted(totals.items()):
        grand += t["usd"]
        print(
            f"{prov + '/' + model:<28} {t['trials']:>7} {t['in']:>10,} "
            f"{t['cached']:>10,} {t['out']:>9,} {t['usd']:>9.4f}"
        )
    print("-" * 78)
    print(f"{'total':<28} {'':>7} {'':>10} {'':>10} {'':>9} {grand:>9.4f}")
    print()
    print("Rates come from bench/pricing.yaml. Any provider marked verified: false there")
    print("makes the USD column an estimate, not a bill. Run `bench.cli check-pricing`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
