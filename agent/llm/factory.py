"""Pick the LLM provider from LLM_PROVIDER (openai | anthropic | bedrock)."""

import os

from dotenv import load_dotenv

from . import anthropic_provider, bedrock_provider, openai_provider

load_dotenv()

SUPPORTED_PROVIDERS = {
    "openai": openai_provider.build,
    "anthropic": anthropic_provider.build,
    "bedrock": bedrock_provider.build,
}


def get_llm(provider: str | None = None, model: str | None = None,
            max_tokens: int | None = None):
    provider = (provider or os.getenv("LLM_PROVIDER", "openai")).lower()
    max_tokens = max_tokens or int(os.getenv("LLM_MAX_TOKENS", "300"))

    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unknown provider '{provider}'. Use one of: {', '.join(SUPPORTED_PROVIDERS)}"
        )
    return SUPPORTED_PROVIDERS[provider](max_tokens, model)
