"""Resolved configuration and the arm registry.

Every result file records the output of `Settings.fingerprint()` so a number can always be
traced back to the config, model, pricing table and seed that produced it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .arms.base import Arm
from .arms.direct import DirectArm
from .arms.mcp_arm import McpArm

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

#: The subset of tools each task genuinely needs, used by the mcp_filtered arm and by the
#: wrong-tool-rate metric. Kept here rather than in the arm so both consumers agree.
RELEVANT_TOOLS_DEFAULT = [
    "list_tickets",
    "get_ticket",
    "assign_ticket",
    "set_ticket_priority",
    "set_ticket_status",
    "list_customers",
    "get_customer",
    "list_inventory",
    "adjust_inventory",
    "list_audit",
    "get_oncall",
]


@dataclass
class Settings:
    backend_url: str = os.getenv("BACKEND_URL", "http://127.0.0.1:9110")
    mcp_sidecar_url: str = os.getenv("MCP_SIDECAR_URL", "http://127.0.0.1:9111/mcp")
    mcp_remote_url: str = os.getenv("MCP_REMOTE_URL", "http://127.0.0.1:9112/mcp")
    seed: int = int(os.getenv("BENCH_SEED", "1729"))
    backend_latency_ms: float = float(os.getenv("BACKEND_LATENCY_MS", "40"))
    backend_jitter_ms: float = float(os.getenv("BACKEND_JITTER_MS", "0"))
    netem_delay: str = os.getenv("NETEM_DELAY", "25ms")
    pricing_path: Path = ROOT / "bench" / "pricing.yaml"
    results_dir: Path = ROOT / "results"

    def pricing(self) -> dict[str, Any]:
        with open(self.pricing_path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def fingerprint(self) -> dict[str, Any]:
        """Config stamp written to every result file.

        Paths are recorded relative to the repo root. Absolute paths would embed the
        operator's home directory and username in results that are meant to be published,
        which is a needless disclosure in an otherwise shareable artifact.
        """
        data: dict[str, Any] = {}
        for k, v in asdict(self).items():
            if isinstance(v, Path):
                try:
                    data[k] = v.relative_to(ROOT).as_posix()
                except ValueError:
                    data[k] = v.name
            else:
                data[k] = v
        data["git_sha"] = _git_sha()
        data["pricing_version"] = self.pricing().get("version")
        data["python"] = sys.version.split()[0]
        return data


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - a missing git is not a benchmark failure
        return "unknown"


SETTINGS = Settings()

ARM_NAMES = ["direct", "mcp_stdio", "mcp_sidecar", "mcp_remote", "mcp_filtered"]


def build_arm(
    name: str,
    *,
    session_reuse: bool = True,
    tool_filter: list[str] | None = None,
    settings: Settings | None = None,
) -> Arm:
    """Construct an arm by name.

    `tool_filter` is honoured by every arm but only *used* by mcp_filtered in the default
    suite. Passing it to the others is how you run the "what if the gateway were curated"
    counterfactual.
    """
    s = settings or SETTINGS

    if name == "direct":
        return DirectArm(tool_filter=tool_filter, base_url=s.backend_url)

    if name == "mcp_stdio":
        return McpArm(
            name="mcp_stdio",
            transport="stdio",
            command=sys.executable,
            args=["-m", "bench.mcpserver.server", "--transport", "stdio"],
            env={**os.environ, "BACKEND_URL": s.backend_url},
            session_reuse=session_reuse,
            tool_filter=tool_filter,
        )

    if name == "mcp_sidecar":
        return McpArm(
            name="mcp_sidecar",
            transport="http",
            url=s.mcp_sidecar_url,
            session_reuse=session_reuse,
            tool_filter=tool_filter,
        )

    if name == "mcp_remote":
        return McpArm(
            name="mcp_remote",
            transport="http",
            url=s.mcp_remote_url,
            session_reuse=session_reuse,
            tool_filter=tool_filter,
        )

    if name == "mcp_filtered":
        # The MCP steelman: same remote gateway, but only the task's relevant tools are
        # loaded into context. If tool_filter is None the caller has not narrowed
        # anything, and this arm is identical to mcp_remote by construction.
        return McpArm(
            name="mcp_filtered",
            transport="http",
            url=s.mcp_remote_url,
            session_reuse=session_reuse,
            tool_filter=tool_filter or RELEVANT_TOOLS_DEFAULT,
        )

    raise ValueError(f"unknown arm: {name!r}. Known arms: {', '.join(ARM_NAMES)}")
