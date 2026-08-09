"""Anthropic provider. The only backend with explicit cache-breakpoint control.

That control is why this provider carries the cache experiment: you can place
`cache_control` on the last tool definition and cache the whole tool block deliberately,
then measure the delta. Everywhere else you can only observe whether a cache happened to
hit.

Two model-family constraints are handled here rather than left as a footgun:

* Sampling parameters (`temperature`, `top_p`, `top_k`) are rejected on the current Opus
  and Sonnet 5 families, so none are sent. Determinism comes from the task design, not
  from temperature, and temperature 0 never guaranteed identical outputs anyway.
* `max_tokens` caps thinking plus response text together, and thinking is on by default
  on Claude Opus 5. The default here is generous so a long tool-selection turn does not
  truncate mid-answer and get scored as a task failure.
"""

from __future__ import annotations

import os
import time
from typing import Any

import anthropic

from ..arms.base import ToolResult, ToolSpec
from .base import ToolCall, TurnResult, Usage


class AnthropicProvider:
    name = "anthropic"
    supports_explicit_cache = True

    def __init__(
        self,
        model: str | None = None,
        effort: str | None = None,
        max_tokens: int = 8192,
    ) -> None:
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-opus-5")
        self.effort = effort or os.getenv("ANTHROPIC_EFFORT", "medium")
        self.max_tokens = max_tokens
        self._client = anthropic.AsyncAnthropic()

    # -- conversation ------------------------------------------------------

    def new_conversation(self, system: str, user: str) -> dict[str, Any]:
        return {"system": system, "messages": [{"role": "user", "content": user}]}

    def _tool_block(self, tools: list[ToolSpec], cache_tools: bool) -> list[dict[str, Any]]:
        blocks = [t.to_anthropic() for t in tools]
        if cache_tools and blocks:
            # Render order is tools -> system -> messages, so a breakpoint on the last
            # tool caches the entire tool block and nothing after it. That isolation is
            # exactly what the experiment needs.
            blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        return blocks

    async def turn(
        self,
        conversation: dict[str, Any],
        tools: list[ToolSpec],
        cache_tools: bool = False,
    ) -> TurnResult:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": conversation["system"],
            "messages": conversation["messages"],
            "output_config": {"effort": self.effort},
        }
        if tools:
            kwargs["tools"] = self._tool_block(tools, cache_tools)

        t0 = time.perf_counter()
        try:
            resp = await self._client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - a provider error is a recorded outcome
            return TurnResult(
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                error=f"{type(exc).__name__}: {exc}",
                stop_reason="error",
            )
        latency_ms = (time.perf_counter() - t0) * 1000.0

        # Safety classifiers can decline with HTTP 200. Check before reading content.
        if resp.stop_reason == "refusal":
            return TurnResult(
                usage=_usage_from(resp.usage),
                stop_reason="refusal",
                latency_ms=latency_ms,
                error="model declined the request",
            )

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, args=args))

        # Echo the full content back, including any thinking blocks, unmodified.
        conversation["messages"].append({"role": "assistant", "content": resp.content})

        return TurnResult(
            text="".join(text_parts),
            tool_calls=calls,
            usage=_usage_from(resp.usage),
            stop_reason=resp.stop_reason or "",
            latency_ms=latency_ms,
        )

    def add_tool_results(
        self,
        conversation: dict[str, Any],
        turn: TurnResult,
        results: list[ToolResult],
    ) -> None:
        # All tool results for one assistant turn go back in a single user message.
        # Splitting them trains the model to stop making parallel calls.
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": result.content,
                **({"is_error": True} if result.is_error else {}),
            }
            for call, result in zip(turn.tool_calls, results)
        ]
        conversation["messages"].append({"role": "user", "content": blocks})

    # -- measurement -------------------------------------------------------

    async def count_tool_tokens(self, tools: list[ToolSpec]) -> int:
        """Exact tool-block size via the count-tokens endpoint, by differencing."""
        probe = [{"role": "user", "content": "ok"}]
        base = await self._client.messages.count_tokens(model=self.model, messages=probe)
        with_tools = await self._client.messages.count_tokens(
            model=self.model,
            messages=probe,
            tools=[t.to_anthropic() for t in tools],
        )
        return max(0, int(with_tools.input_tokens) - int(base.input_tokens))

    async def aclose(self) -> None:
        await self._client.close()


def _usage_from(raw: Any) -> Usage:
    if raw is None:
        return Usage()
    return Usage(
        input_tokens=int(getattr(raw, "input_tokens", 0) or 0),
        output_tokens=int(getattr(raw, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(raw, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(raw, "cache_creation_input_tokens", 0) or 0),
    )
