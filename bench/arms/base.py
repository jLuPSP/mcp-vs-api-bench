"""Arm interface: the one thing the benchmark varies.

An arm is a way of reaching tools. It advertises tool specs and executes tool calls.
Everything downstream of an arm (model, tasks, grading, accounting) is identical, so any
measured difference is attributable to the access layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

UPSTREAM_KEY = "_bench_upstream_ms"


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the model will see it."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ToolResult:
    """Outcome of one tool call.

    `content` is what the model sees. `upstream_ms` is the backend's own view of the time
    it spent, stripped out of the content so token counts stay identical across arms.
    """

    content: str
    upstream_ms: float = 0.0
    is_error: bool = False


@dataclass
class SessionCost:
    """Cost of establishing a tool session: connect, initialize, tools/list."""

    connect_ms: float = 0.0
    initialize_ms: float = 0.0
    list_tools_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.connect_ms + self.initialize_ms + self.list_tools_ms


def split_upstream(raw: str) -> tuple[str, float]:
    """Pull benchmark instrumentation out of a tool payload.

    Returns the payload the model should see, and the upstream service time. If the
    instrumentation key is absent the payload passes through untouched.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return raw, 0.0
    if not isinstance(obj, dict) or UPSTREAM_KEY not in obj:
        return raw, 0.0
    upstream = float(obj.pop(UPSTREAM_KEY) or 0.0)
    inner = obj.get("result", obj)
    return json.dumps(inner), upstream


class Arm:
    """Base class. Subclasses implement connect / list_tools / call_tool / close."""

    name: str = "base"
    #: Set by subclasses. Recorded in results so the report can group by transport.
    transport: str = "none"

    def __init__(self, tool_filter: list[str] | None = None) -> None:
        #: Optional allowlist. Used by the mcp_filtered arm to load only relevant tools.
        self.tool_filter = tool_filter
        self.session_cost = SessionCost()
        self._tools: list[ToolSpec] = []

    async def __aenter__(self) -> "Arm":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def connect(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def list_tools(self) -> list[ToolSpec]:  # pragma: no cover - interface
        raise NotImplementedError

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:  # pragma: no cover
        raise NotImplementedError

    def _apply_filter(self, tools: list[ToolSpec]) -> list[ToolSpec]:
        if not self.tool_filter:
            return tools
        allowed = set(self.tool_filter)
        return [t for t in tools if t.name in allowed]
