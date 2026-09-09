"""Privacy boundaries for provider metadata projected to destinations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

_SENSITIVE_KEYS = frozenset(
    {
        "iban",
        "account_number",
        "accountnumber",
        "pan",
    }
)


def redact_destination_metadata(
    value: Any,
    *,
    allow_raw_payload: bool = False,
    allow_attachment_content: bool = False,
) -> Any:
    """Return a JSON-safe copy with sensitive fields removed by default.

    Destination projections receive only explicitly shaped metadata.  Even
    when a provider puts a sensitive field inside a nested extension object,
    the default projection must not accidentally forward it.
    """
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in cast("Mapping[Any, Any]", value).items():
            normalized = str(key).casefold().replace("-", "_")
            if (
                normalized in {"raw_payload", "rawpayload"}
                and not allow_raw_payload
            ):
                continue
            if (
                normalized in {"attachment_content", "attachmentcontent"}
                and not allow_attachment_content
            ):
                continue
            if normalized in _SENSITIVE_KEYS:
                continue
            result[str(key)] = redact_destination_metadata(
                item,
                allow_raw_payload=allow_raw_payload,
                allow_attachment_content=allow_attachment_content,
            )
        return result
    if isinstance(value, list):
        return [
            redact_destination_metadata(
                item,
                allow_raw_payload=allow_raw_payload,
                allow_attachment_content=allow_attachment_content,
            )
            for item in cast("list[Any]", value)
        ]
    if isinstance(value, tuple):
        return [
            redact_destination_metadata(
                item,
                allow_raw_payload=allow_raw_payload,
                allow_attachment_content=allow_attachment_content,
            )
            for item in cast("tuple[Any, ...]", value)
        ]
    return value
