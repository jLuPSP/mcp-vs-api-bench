"""Model provider interface with normalised token accounting.

Token fields differ across providers (Anthropic splits cache writes from cache reads;
DeepSeek reports cache hit/miss on the prompt; Ollama reports neither). They are
normalised here so the cost model has one shape to price.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Protocol

from ..arms.base import ToolResult, ToolSpec


@dataclass
class Usage:
    """Normalised per-turn token usage."""

    input_tokens: int = 0           # uncached input, billed at full rate
    output_tokens: int = 0
    cache_read_tokens: int = 0      # served from cache, billed at the read rate
    cache_write_tokens: int = 0     # written to cache, billed at the write rate

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class TurnResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""
    latency_ms: float = 0.0
    error: str | None = None


class Provider(Protocol):
    """Owns its own message format. The harness never touches provider message dicts."""

    name: str
    model: str
    supports_explicit_cache: bool

    def new_conversation(self, system: str, user: str) -> Any: ...

    async def turn(
        self,
        conversation: Any,
        tools: list[ToolSpec],
        cache_tools: bool = False,
    ) -> TurnResult: ...

    def add_tool_results(
        self,
        conversation: Any,
        turn: TurnResult,
        results: list[ToolResult],
    ) -> None: ...

    async def count_tool_tokens(self, tools: list[ToolSpec]) -> int:
        """Exact token cost of the tool block, measured not estimated."""
        ...

    async def aclose(self) -> None: ...
