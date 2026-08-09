"""OpenAI-compatible provider. Covers DeepSeek and Ollama.

DeepSeek's context caching is automatic (a disk-backed prefix cache), so there is no
breakpoint to place and `cache_tools` is a no-op here. Its usage payload reports
`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`, which map cleanly onto the
normalised Usage fields. Ollama reports neither, so everything lands in `input_tokens`.

That structural difference matters for the cache experiment: on DeepSeek you measure
whether the cache happens to hit, whereas on Anthropic you measure the effect of
deliberately placing a breakpoint on the tool block. Report them as separate rows, never
averaged together.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from openai import AsyncOpenAI

from ..arms.base import ToolResult, ToolSpec
from .base import ToolCall, TurnResult, Usage

PRESETS = {
    "deepseek": {
        "base_url_env": "DEEPSEEK_BASE_URL",
        "base_url_default": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "key_required": True,
        "model_env": "DEEPSEEK_MODEL",
        "model_default": "deepseek-chat",
    },
    "ollama": {
        "base_url_env": "OLLAMA_URL",
        "base_url_default": "http://127.0.0.1:11434/v1",
        "key_env": "OLLAMA_API_KEY",
        # Ollama ignores the key, but the OpenAI SDK requires a non-empty string.
        "key_required": False,
        "model_env": "OLLAMA_MODEL",
        "model_default": "qwen3:14b-16k",
    },
}


class OpenAICompatProvider:
    supports_explicit_cache = False

    def __init__(self, name: str, model: str | None = None, temperature: float = 0.0) -> None:
        if name not in PRESETS:
            raise ValueError(f"unknown openai-compatible provider: {name}")
        preset = PRESETS[name]
        self.name = name
        self.model = model or os.getenv(preset["model_env"], preset["model_default"])
        self.temperature = temperature
        base_url = os.getenv(preset["base_url_env"], preset["base_url_default"])
        api_key = os.getenv(preset["key_env"], "").strip()
        if not api_key:
            if preset["key_required"]:
                # Falling back to a placeholder here produces a 401 whose message names
                # the placeholder, which reads as "your key is wrong" rather than "you
                # have no key". Fail with the actual problem instead.
                raise RuntimeError(
                    f"{preset['key_env']} is not set. Put it in .env (gitignored) or "
                    f"export it, then re-run. Provider '{name}' requires a key."
                )
            api_key = "not-needed"
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=300.0)

    # -- conversation ------------------------------------------------------

    def new_conversation(self, system: str, user: str) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    async def turn(
        self,
        conversation: list[dict[str, Any]],
        tools: list[ToolSpec],
        cache_tools: bool = False,
    ) -> TurnResult:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": conversation,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = [t.to_openai() for t in tools]
            kwargs["tool_choice"] = "auto"

        t0 = time.perf_counter()
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - a provider error is a recorded outcome
            return TurnResult(
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                error=f"{type(exc).__name__}: {exc}",
                stop_reason="error",
            )
        latency_ms = (time.perf_counter() - t0) * 1000.0

        choice = resp.choices[0]
        msg = choice.message
        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))

        # Echo the assistant turn back into history exactly as received.
        assistant: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        conversation.append(assistant)

        return TurnResult(
            text=msg.content or "",
            tool_calls=calls,
            usage=_usage_from(resp.usage),
            stop_reason=choice.finish_reason or "",
            latency_ms=latency_ms,
        )

    def add_tool_results(
        self,
        conversation: list[dict[str, Any]],
        turn: TurnResult,
        results: list[ToolResult],
    ) -> None:
        for call, result in zip(turn.tool_calls, results):
            conversation.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result.content,
                }
            )

    # -- measurement -------------------------------------------------------

    async def count_tool_tokens(self, tools: list[ToolSpec]) -> int:
        """Measure the tool block by differencing prompt tokens on two probe requests.

        There is no count-tokens endpoint here, so the only honest measurement is to ask
        the provider what it charged. Deliberately not tiktoken: that is OpenAI's
        tokenizer and is wrong by a wide margin for these models.
        """
        probe = [{"role": "user", "content": "ok"}]
        base = await self._probe_prompt_tokens(probe, None)
        with_tools = await self._probe_prompt_tokens(probe, tools)
        return max(0, with_tools - base)

    async def _probe_prompt_tokens(
        self, messages: list[dict[str, Any]], tools: list[ToolSpec] | None
    ) -> int:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 1,
            "temperature": 0.0,
        }
        if tools:
            kwargs["tools"] = [t.to_openai() for t in tools]
        resp = await self._client.chat.completions.create(**kwargs)
        return int(getattr(resp.usage, "prompt_tokens", 0) or 0)

    async def aclose(self) -> None:
        await self._client.close()


def _usage_from(raw: Any) -> Usage:
    if raw is None:
        return Usage()
    prompt = int(getattr(raw, "prompt_tokens", 0) or 0)
    completion = int(getattr(raw, "completion_tokens", 0) or 0)
    # DeepSeek-specific cache accounting; absent elsewhere.
    hit = int(getattr(raw, "prompt_cache_hit_tokens", 0) or 0)
    miss = getattr(raw, "prompt_cache_miss_tokens", None)
    if hit or miss is not None:
        uncached = int(miss) if miss is not None else max(0, prompt - hit)
        return Usage(input_tokens=uncached, output_tokens=completion, cache_read_tokens=hit)
    return Usage(input_tokens=prompt, output_tokens=completion)
