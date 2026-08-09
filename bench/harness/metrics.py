"""Statistics and result persistence.

Deliberately conservative: percentiles rather than means for latency (the distributions
are right-skewed and a mean hides the tail that pages people), bootstrap CIs rather than
standard errors, Wilson intervals for success rates, and no p-values anywhere. These
comparisons are not hypothesis tests and dressing them as such would overstate the rigor.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np


@dataclass
class LatencyStats:
    n: int
    p50: float
    p95: float
    p99: float
    mean: float
    ci_low: float      # bootstrap 95% CI on the median
    ci_high: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def latency_stats(samples: Sequence[float], resamples: int = 10_000, seed: int = 1729) -> LatencyStats:
    if not samples:
        return LatencyStats(0, 0, 0, 0, 0, 0, 0)
    arr = np.asarray(samples, dtype=float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(resamples, len(arr)))
    medians = np.median(arr[idx], axis=1)
    return LatencyStats(
        n=len(arr),
        p50=float(np.percentile(arr, 50)),
        p95=float(np.percentile(arr, 95)),
        p99=float(np.percentile(arr, 99)),
        mean=float(arr.mean()),
        ci_low=float(np.percentile(medians, 2.5)),
        ci_high=float(np.percentile(medians, 97.5)),
    )


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """Wilson score interval. Returns (point, low, high) as proportions.

    Used instead of a bare success percentage because at n=25 a four point difference is
    inside the noise, and the report needs to be able to say so.
    """
    if n == 0:
        return 0.0, 0.0, 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return p, max(0.0, centre - half), min(1.0, centre + half)


def token_cost(
    usage: dict[str, int],
    provider: str,
    model: str,
    pricing: dict[str, Any],
) -> float:
    """Dollars for one usage record, honouring separate cached and uncached rates."""
    prov = pricing.get("providers", {}).get(provider)
    if not prov:
        return 0.0
    spec = prov.get("models", {}).get(model)
    if not spec:
        return 0.0

    per_m = 1_000_000.0
    inp = spec.get("input_per_mtok", 0.0)
    out = spec.get("output_per_mtok", 0.0)
    cache = prov.get("cache", {})

    # A provider may quote a cached input rate directly (DeepSeek) or as a multiplier on
    # the base input rate (Anthropic). Prefer the explicit rate when present.
    cached_rate = spec.get("input_cached_per_mtok")
    if cached_rate is None:
        cached_rate = inp * cache.get("read", 1.0)
    write_rate = inp * cache.get("write_5m", 1.0)

    return (
        usage.get("input_tokens", 0) * inp
        + usage.get("cache_read_tokens", 0) * cached_rate
        + usage.get("cache_write_tokens", 0) * write_rate
        + usage.get("output_tokens", 0) * out
    ) / per_m


def cacheability_warning(
    tool_tokens: int, provider: str, model: str, pricing: dict[str, Any]
) -> str | None:
    """Flag a tool block that is too small to be cacheable at all.

    This is the finding people miss, and it inverts the intuition: a tight hand-written
    3-tool block is often BELOW the minimum cacheable prefix and therefore can never
    benefit from caching, while the bloated 40-tool gateway block is the only one large
    enough to qualify.
    """
    prov = pricing.get("providers", {}).get(provider, {})
    spec = prov.get("models", {}).get(model, {})
    minimum = spec.get("min_cacheable_tokens")
    if not minimum:
        return None
    if tool_tokens < minimum:
        return (
            f"tool block is {tool_tokens} tokens, below the {minimum}-token minimum "
            f"cacheable prefix for {model}: it will silently never cache"
        )
    return None


class ResultWriter:
    """Append-only JSONL, one file per run, with the config fingerprint on line 1.

    A result without a fingerprint is not reproducible, so writing one is not optional.
    """

    def __init__(self, results_dir: Path, kind: str, fingerprint: dict[str, Any]) -> None:
        results_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = results_dir / f"{kind}-{stamp}.jsonl"
        self._fh = self.path.open("w", encoding="utf-8")
        self.write({"record": "meta", "kind": kind, "started_at": stamp, "config": fingerprint})

    def write(self, row: Any) -> None:
        if is_dataclass(row) and not isinstance(row, type):
            row = asdict(row)
        self._fh.write(json.dumps(row, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "ResultWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def latest(results_dir: Path, kind: str) -> Path | None:
    files = sorted(results_dir.glob(f"{kind}-*.jsonl"))
    return files[-1] if files else None
