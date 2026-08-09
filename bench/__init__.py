"""Benchmark package.

`.env` is loaded here, at package import, rather than in `bench.config`. Loading it in
config meant any code path that touched a provider without importing config ran with no
credentials at all, and the placeholder key produced a 401 that looked like a bad key
rather than a missing one. Loading at the package root makes it unconditional.

Ambient environment variables deliberately win over `.env`, which is the conventional
precedence and lets CI inject credentials without rewriting files.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env", override=False)
