"""Result value objects returned by sync orchestration."""

from __future__ import annotations

from datetime import datetime

from finance_sync.models.enums import ReconciliationRunStatus, SyncRunStatus


class SyncResult:
    """Outcome of a single sync run."""

    __slots__ = (
        "accounts_synced",
        "duration_s",
        "error_category",
        "error_message",
        "holdings_synced",
        "rate_limit_attempts",
        "rate_limit_scope",
        "retry_after_at",
        "status",
        "transactions_synced",
        "unresolved_securities",
    )

    def __init__(
        self,
        *,
        status: SyncRunStatus,
        accounts_synced: int,
        transactions_synced: int,
        error_message: str | None,
        duration_s: float,
        holdings_synced: int = 0,
        unresolved_securities: int = 0,
        error_category: str | None = None,
        retry_after_at: datetime | None = None,
        rate_limit_scope: str | None = None,
        rate_limit_attempts: int = 0,
    ) -> None:
        self.status = status
        self.accounts_synced = accounts_synced
        self.transactions_synced = transactions_synced
        self.holdings_synced = holdings_synced
        self.unresolved_securities = unresolved_securities
        self.error_message = error_message
        self.error_category = error_category
        self.retry_after_at = retry_after_at
        self.rate_limit_scope = rate_limit_scope
        self.rate_limit_attempts = rate_limit_attempts
        self.duration_s = duration_s

    def __repr__(self) -> str:
        return (
            f"<SyncResult status={self.status!r} "
            f"accts={self.accounts_synced} txns={self.transactions_synced} "
            f"holdings={self.holdings_synced} "
            f"unresolved={self.unresolved_securities} "
            f"err={self.error_message!r} dur={self.duration_s:.2f}s>"
        )


class ReconciliationRunSummary:
    """Outcome of a reconciliation analysis run."""

    __slots__ = (
        "finding_count",
        "run_id",
        "status",
    )

    def __init__(
        self,
        *,
        run_id: str,
        status: ReconciliationRunStatus,
        finding_count: int,
    ) -> None:
        self.run_id = run_id
        self.status = status
        self.finding_count = finding_count

    def __repr__(self) -> str:
        return (
            f"<ReconciliationRunSummary run_id={self.run_id!r} "
            f"status={self.status!r} findings={self.finding_count}>"
        )
