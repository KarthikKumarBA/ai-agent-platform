"""Helpers shared by all providers."""


def response_text(message) -> str:
    """Return plain text from any provider's response (string or content blocks)."""
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
