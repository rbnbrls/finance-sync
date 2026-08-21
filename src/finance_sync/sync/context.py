"""Immutable context shared by sync stages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SyncContext:
    """Stable identity and time inputs for one sync transaction."""

    tenant_id: str
    provider_type: str
    since: datetime
    connection_id: str | None = None
    sync_run_id: str | None = None
