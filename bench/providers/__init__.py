from __future__ import annotations

from .base import Provider, ToolCall, TurnResult, Usage

#: `oracle` is a scripted perfect agent, not a model. It exists so the harness can be
#: tested end to end with no API key and no spend, and so a failure can be attributed to
#: the arm rather than the model. It always passes, so it is a ceiling, not a baseline.
PROVIDER_NAMES = ["ollama", "deepseek", "anthropic", "oracle"]


def build_provider(name: str, model: str | None = None) -> Provider:
    if name == "oracle":
        from .oracle import OracleProvider

        return OracleProvider(model=model)
    if name in ("deepseek", "ollama"):
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(name, model=model)
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=model)
    raise ValueError(f"unknown provider: {name!r}. Known: {', '.join(PROVIDER_NAMES)}")


__all__ = ["Provider", "ToolCall", "TurnResult", "Usage", "build_provider", "PROVIDER_NAMES"]
