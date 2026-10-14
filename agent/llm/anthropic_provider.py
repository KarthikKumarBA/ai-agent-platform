"""Anthropic Claude API provider."""

import os

from langchain_anthropic import ChatAnthropic


def build(max_tokens: int, model: str | None = None) -> ChatAnthropic:
    return ChatAnthropic(
        model=model or os.getenv("ANTHROPIC_MODEL_ID", "claude-haiku-4-5"),
        max_tokens=max_tokens,
    )
