"""Remove secrets from text before it is sent to the LLM."""

import re

_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bABSK[A-Za-z0-9+/=]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-~+/]+=*"), r"\1 [REDACTED]"),
    (re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
        r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"),
     r"\1\2[REDACTED]"),
]

_SENSITIVE_ENV_NAME = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)"
)


def redact_text(text: str | None) -> str | None:
    """Replace known secret patterns in free text (logs, event messages)."""
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_env(env: dict) -> dict:
    """Hide values of secret-looking env vars; scrub the rest."""
    return {
        name: "[REDACTED]" if _SENSITIVE_ENV_NAME.search(name) else redact_text(value)
        for name, value in env.items()
    }
