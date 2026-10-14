"""OpenAI API provider."""

import os

from langchain_openai import ChatOpenAI


def build(max_tokens: int, model: str | None = None) -> ChatOpenAI:
    model = model or os.getenv("OPENAI_MODEL_ID", "gpt-5.6-luna")

    # GPT-5.x are reasoning models: use Responses API with low effort to save tokens
    if model.startswith("gpt-5"):
        return ChatOpenAI(
            model=model,
            use_responses_api=True,
            reasoning={"effort": "low"},
            max_tokens=max_tokens,
        )

    # Older models (e.g. gpt-4o-mini) support temperature
    return ChatOpenAI(model=model, temperature=0, max_tokens=max_tokens)
