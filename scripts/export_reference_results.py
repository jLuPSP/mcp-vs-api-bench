"""Copy results/ into results-reference/ for publication, scrubbing local paths.

`results/` is gitignored so that routine runs do not churn the repo. But a benchmark
whose raw data ships with it is auditable and one whose data does not is a press release,
so the exact files backing the published claims are committed under `results-reference/`.

The only transformation is removing absolute paths from the config fingerprint. Those
embed the operator's home directory and username, which is a needless disclosure in an
artifact meant to be shared. No measurement value is altered.

    python -m scripts.export_reference_results
    python -m scripts.export_reference_results --check   # verify, change nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "results"
DST = ROOT / "results-reference"

PATH_KEYS = ("pricing_path", "results_dir")


def looks_absolute(value: str) -> bool:
    return "\\" in value or value.startswith("/") or (len(value) > 1 and value[1] == ":")


def scrub(row: dict) -> tuple[dict, int]:
    cfg = row.get("config")
    hits = 0
    if isinstance(cfg, dict):
        for key in PATH_KEYS:
            val = cfg.get(key)
            if isinstance(val, str) and looks_absolute(val):
                cfg[key] = os.path.basename(val.replace("\\", "/"))
                hits += 1
    return row, hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="export publishable reference results")
    parser.add_argument("--check", action="store_true", help="report only, write nothing")
    args = parser.parse_args(argv)

    if not SRC.exists():
        print(f"no {SRC.name}/ directory; nothing to export")
        return 0

    DST.mkdir(exist_ok=True)
    files = sorted(SRC.glob("*.jsonl"))
    total_scrubbed = 0
    written = 0

    for src in files:
        rows = []
        scrubbed = 0
        for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            row, hits = scrub(row)
            scrubbed += hits
            rows.append(json.dumps(row))
        total_scrubbed += scrubbed
        if not args.check:
            (DST / src.name).write_text("\n".join(rows) + "\n", encoding="utf-8")
            written += 1

    verb = "would scrub" if args.check else "scrubbed"
    print(f"{len(files)} result files, {verb} {total_scrubbed} absolute paths")
    if not args.check:
        print(f"wrote {written} files to {DST.name}/")

    # Fail loudly if anything identifying survived, since this runs before a public push.
    leaked = []
    target = SRC if args.check else DST
    home = Path.home().name.lower()
    for f in target.glob("*.jsonl"):
        text = f.read_text(encoding="utf-8", errors="replace").lower()
        if home and home in text:
            leaked.append(f.name)
    if leaked and not args.check:
        print(f"ERROR: username still present in {len(leaked)} file(s): {leaked[:3]}", file=sys.stderr)
        return 1
    print("no username found in exported results" if not args.check else "(check mode)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
