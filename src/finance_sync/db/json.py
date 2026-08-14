"""JSON serialization helpers for PostgreSQL JSONB columns.

finance-sync stores rich domain payloads in JSONB (outbox messages,
provider metadata, price payloads).  These payloads legitimately contain
:class:`decimal.Decimal` amounts, tz-aware datetimes and UUIDs, none of
which the stdlib ``json`` encoder can serialize by default.

Wire this into any engine that writes JSONB:

    from finance_sync.db.json import default_json_serializer

    create_async_engine(url, json_serializer=default_json_serializer)

The same serializer must be used by the integration test harness so tests
match production behaviour.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


def default_json_serializer(obj: Any) -> str:
    """Serialize ``obj`` to JSON, coercing non-JSON-native types.

    * :class:`decimal.Decimal` → ``str`` (lossless; JSON has no decimal)
    * :class:`datetime.datetime` / :class:`datetime.date` → ISO 8601
    * :class:`uuid.UUID` → canonical string
    * everything else falls back to the stdlib encoder
    """

    def _default(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, UUID):
            return str(value)
        msg = (
            f"Object of type {value.__class__.__name__} "
            "is not JSON serializable"
        )
        raise TypeError(msg)

    return json.dumps(obj, default=_default)
