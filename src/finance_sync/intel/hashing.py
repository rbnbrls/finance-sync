"""Content hashing for market-intelligence items.

A content hash lets us deduplicate syndicated items across providers
and verify that a stored observation still matches what the source
served.  The hash is computed over the *canonical identity* of an
item — the pieces that make it the same story regardless of which
provider syndicated it — never over the full body text (which we may
not be licensed to persist).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def content_hash(*parts: Any) -> str:
    """Return a stable SHA-256 hex digest over *parts*.

    Parts are JSON-normalised (``sort_keys``, ``default=str``) so that
    dicts with equivalent keys hash identically regardless of insertion
    order and ``Decimal``/``datetime`` values serialise deterministically.

    Usage::

        content_hash(provider="sec", source_id="0000320193-26-000123")
        # e.g. "9f2c…"
    """
    normalised: list[str] = []
    for part in parts:
        if isinstance(part, (dict, list, tuple)):
            normalised.append(
                json.dumps(
                    part, sort_keys=True, default=str, separators=(",", ":")
                )
            )
        else:
            normalised.append(str(part))
    joined = "\x1f".join(normalised)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
