"""License-class inference and snippet policy for the source layer.

Providers sometimes report a free-form license string (or none at all)
instead of a structured reuse class.  We never trust that string: the
inference below maps it to one of the canonical
:class:`IntelLicenseClass` values, and anything unrecognised — empty,
``"copyright (c) 2026"``, ``"CC-BY-NC-4.0"`` instead of ``"CC BY-NC 4.0"``
— falls back to :data:`IntelLicenseClass.PROPRIETARY` (metadata +
structured facts only; never snippets, never full text).

The snippet cap is enforced in *characters* (not bytes) so multi-byte
content (emoji, CJK) can never smuggle extra text past the limit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from finance_sync.intel.enums import (
    FULL_CONTENT_LICENSE_CLASSES,
    SNIPPET_LICENSE_CLASSES,
    IntelLicenseClass,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Default maximum snippet length in characters (applies to every class).
DEFAULT_SNIPPET_MAX_CHARS = 500

#: Keywords that mark an unambiguous open/public reuse class.
_OPEN_PATTERNS: Sequence[tuple[tuple[str, ...], IntelLicenseClass]] = (
    (
        ("public domain", "publicdomain", "cc0", "no copyright"),
        IntelLicenseClass.PUBLIC_DOMAIN,
    ),
    (
        (
            "cc by",
            "creative commons attribution",
            "cc-by",
            "cc_by",
            "open license",
            "open licence",
            "apache 2.0",
            "mit license",
            "bsd",
        ),
        IntelLicenseClass.OPEN_LICENSE,
    ),
)

#: Keywords that mark a *restricted* reuse class (never full text).
_RESTRICTED_PATTERNS: Sequence[tuple[str, ...]] = (
    ("cc by-nc", "cc-by-nc", "non-commercial", "nc license"),
    ("subscription", "subscriber", "paywall", "paid", "proprietary"),
    ("copyright", "all rights reserved", "©"),
    ("terms of service", "terms of use", "license agreement"),
)

#: When the string is empty or cannot be classified at all.
_FALLBACK = IntelLicenseClass.PROPRIETARY


def infer_license_class(raw: str | None) -> IntelLicenseClass:
    """Map a free-form license string to a canonical reuse class.

    Classification order (first match wins):

    1. restricted keywords (NC, subscription, copyright…) → ``subscriber_only``
       — checked FIRST so ``"CC BY-NC 4.0"`` / ``"CC-BY-NC-4.0"`` can
       never be mistaken for an open license by the ``cc by`` keyword.
    2. explicit open/public keywords → ``public_domain`` / ``open_license``
    3. anything else (empty, unknown, deviant spelling) → ``proprietary``

    ``proprietary`` is the safe default: only metadata, structured facts
    and a canonical link are storable; snippets and full text are never
    persisted, so an unrecognised license string can never cause a
    copyright violation.
    """
    if not raw:
        return _FALLBACK
    text = " ".join(str(raw).strip().lower().split())

    for keywords in _RESTRICTED_PATTERNS:
        if any(keyword in text for keyword in keywords):
            return IntelLicenseClass.SUBSCRIBER_ONLY

    for keywords, license_class in _OPEN_PATTERNS:
        if any(keyword in text for keyword in keywords):
            return license_class

    return _FALLBACK


def can_store_full_text(license_class: IntelLicenseClass) -> bool:
    """Return True when *license_class* permits persisting full text."""
    return license_class in FULL_CONTENT_LICENSE_CLASSES


def can_store_snippet(license_class: IntelLicenseClass) -> bool:
    """Return True when *license_class* permits persisting a snippet."""
    return license_class in SNIPPET_LICENSE_CLASSES


def enforce_snippet_limit(
    text: str | None,
    *,
    max_chars: int = DEFAULT_SNIPPET_MAX_CHARS,
) -> str | None:
    """Return *text* truncated to *max_chars* characters (not bytes).

    The limit is enforced on the *character* count — a CJK or emoji
    string of 500 characters cannot smuggle extra text past the cap via
    multi-byte UTF-8 encodings.
    """
    if not text:
        return None
    return text[:max_chars] if len(text) > max_chars else text
