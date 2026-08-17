"""Secret redaction helpers.

Provider credentials (API keys, secrets, tokens) must never leak into
logs, API responses, metrics or stored error messages.  These helpers
scrub common secret shapes and explicit credential values from free-form
text before it is persisted or returned.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Replacements for well-known secret shapes.
_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)
#: Long opaque tokens (sk_live_..., ghp_..., base64 blobs, hex digests).
_TOKEN_RE = re.compile(
    r"(?i)(sk|pk|rk|ghp|gho|ghu|glpat|api)[_-][A-Za-z0-9_-]{12,}"
)
#: Bearer / Basic authorization headers (scheme label is preserved).
_AUTH_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(Bearer|Basic)\s+[^\s,;]+")
#: key=value style secrets (password=..., secret=..., token=..., api_key=...).
_KEYVALUE_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|client[_-]?secret"
    r"|access[_-]?key|private[_-]?key)\b\s*[:=]\s*[^\s,;\"']+"
)
#: Very long unbroken runs of base64/hex-ish characters (encryption keys).
_LONG_RUN_RE = re.compile(r"[A-Za-z0-9+/_\-=]{32,}")
#: The string literal used as a replacement.
REDACTED = "[REDACTED]"


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Return *text* with secret values and secret-shaped tokens scrubbed.

    ``secrets`` may carry the tenant's decrypted credential values; each
    value (and any slash-stripped variant) is removed verbatim.
    """
    if not text:
        return text
    redacted = text
    for secret in secrets:
        if not secret or len(str(secret)) < 4:
            continue
        redacted = redacted.replace(str(secret), REDACTED)
        redacted = redacted.replace(str(secret).replace("/", ""), REDACTED)
        # URL-encoded variant (e.g. inside a query string)
        from urllib.parse import quote

        redacted = redacted.replace(quote(str(secret), safe=""), REDACTED)
    redacted = _JWT_RE.sub(REDACTED, redacted)
    redacted = _AUTH_RE.sub(rf"\1\2 {REDACTED}", redacted)
    redacted = _KEYVALUE_RE.sub(rf"\1{REDACTED}", redacted)
    redacted = _TOKEN_RE.sub(REDACTED, redacted)
    return _LONG_RUN_RE.sub(REDACTED, redacted)


def sanitize_error(
    message: str,
    secrets: Iterable[str] = (),
    *,
    max_length: int = 500,
) -> str:
    """Redact secrets from an error message and truncate it.

    The result is safe to persist on the connection row (``last_error``),
    include in API responses, or log.  Errors longer than *max_length*
    are cut at a newline/sentence boundary when possible.
    """
    cleaned = redact_text(str(message or ""), secrets)
    if len(cleaned) <= max_length:
        return cleaned
    truncated = cleaned[:max_length]
    # Try to cut at a meaningful boundary just before the cut point.
    for sep in ("\n", "; ", ". "):
        cut = truncated.rfind(sep)
        if cut > max_length // 2:
            return truncated[: cut + len(sep)].rstrip() + " …"
    return truncated.rstrip() + " …"
