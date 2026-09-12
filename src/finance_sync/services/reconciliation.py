"""Reconciliation service — detects missing / duplicate transactions.

The service performs two types of analysis:

1. **Duplicate detection** — finds pairs of transactions within the same
   account that have identical amounts and close occurrence dates but
   different provider IDs (suggesting the same real-world event was
   ingested twice).

2. **Cross-connector gap detection** — for accounts that are fed by
   multiple connector providers, checks that each provider's transaction
   date range covers the expected period and flags providers that appear
   to be missing data relative to others.

Results are stored as ``ReconciliationResult`` records linked to a
``ReconciliationRun`` so that findings are durable and reviewable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import structlog
from sqlalchemy import select

from finance_sync.duplicate_detection import (
    has_distinct_transaction_ids_in_descriptions,
)
from finance_sync.models import ReconciliationResult, ReconciliationRun
from finance_sync.models.enums import (
    ReconciliationResultKind,
    ReconciliationRunStatus,
    ReconciliationSeverity,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
    )


logger = structlog.get_logger("finance_sync.services.reconciliation")

# ── Severity helpers ──────────────────────────────────────────────────

_DEFAULT_DAYS_BACK = 90  # Default date range for a full scan
_DUPLICATE_THRESHOLD_HOURS = 48  # Max hours apart to consider a duplicate


def _severity(count: int, total: int) -> ReconciliationSeverity:  # type: ignore[reportUnusedFunction]
    """Derive severity from the ratio of findings to total transactions.

    A high finding/total ratio is more severe.
    """
    if total == 0:
        return ReconciliationSeverity.INFO
    ratio = count / total
    if ratio > 0.1:
        return ReconciliationSeverity.ERROR
    if ratio > 0.02:
        return ReconciliationSeverity.WARNING
    return ReconciliationSeverity.INFO


# ── Service ───────────────────────────────────────────────────────────


class ReconciliationService:
    """Detect missing and duplicate transactions across connectors.

    Usage::

        svc = ReconciliationService(
            session_factory=container.session_factory,
            tenant_id="tenant_1",
        )
        run = await svc.reconcile()
        print(f"Found {run.finding_count} issues")
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        tenant_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id

    # ── Public API ───────────────────────────────────────────────────

    async def reconcile(
        self,
        *,
        account_ids: list[str] | None = None,
        provider_keys: list[str] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        threshold_hours: int = _DUPLICATE_THRESHOLD_HOURS,
        detect_duplicates: bool = True,
    ) -> ReconciliationRun:
        """Run a full reconciliation analysis.

        Args:
            account_ids:    Optional subset of accounts to analyze.
            provider_keys:  Optional provider/connector keys to compare.
                            When set, only transactions from these providers
                            are included in the analysis.
            date_from:      Earliest transaction date (default 90 days ago).
            date_to:        Latest transaction date (default now).
            threshold_hours: Max hour gap for duplicate candidates.
            detect_duplicates:
                Whether to run duplicate detection phase (default ``True``).

        Returns:
            The completed ``ReconciliationRun`` with its finding count
            and summary populated.
        """
        _date_to = date_to or datetime.now(UTC)
        _date_from = date_from or _default_since()

        log = logger.bind(
            tenant_id=self._tenant_id,
            date_from=_date_from.isoformat(),
            date_to=_date_to.isoformat(),
        )
        log.info("reconciliation_starting")

        scope: dict[str, Any] = {
            "date_from": _date_from.isoformat(),
            "date_to": _date_to.isoformat(),
        }
        if account_ids:
            scope["account_ids"] = account_ids
        if provider_keys:
            scope["provider_keys"] = provider_keys
        if not detect_duplicates:
            scope["detect_duplicates"] = False

        async with self._session_factory() as session:
            run = ReconciliationRun(
                tenant_id=self._tenant_id,
                status=ReconciliationRunStatus.RUNNING,
                started_at=datetime.now(UTC),
                scope=scope,
            )
            session.add(run)
            await session.flush()

            findings: list[ReconciliationResult] = []

            try:
                # Phase 1: Duplicate detection
                provider_kw: dict[str, Any] = {}
                if provider_keys:
                    provider_kw["provider_keys"] = provider_keys

                if detect_duplicates:
                    dups = await self._detect_duplicates(
                        session,
                        run,
                        account_ids,
                        _date_from,
                        _date_to,
                        threshold_hours,
                        **provider_kw,
                    )
                    findings.extend(dups)
                    log.debug("duplicates_detected", count=len(dups))

                # Phase 2: Cross-connector gap detection
                gaps = await self._detect_cross_connector_gaps(
                    session,
                    run,
                    account_ids,
                    _date_from,
                    _date_to,
                    **provider_kw,
                )
                findings.extend(gaps)
                log.debug("cross_connector_gaps_detected", count=len(gaps))

                # Phase 3: Missing transaction detection across providers
                missing = await self._detect_missing_transactions(
                    session,
                    run,
                    account_ids,
                    _date_from,
                    _date_to,
                    **provider_kw,
                )
                findings.extend(missing)
                log.debug("missing_transactions_detected", count=len(missing))

                # Finalize the run
                await self._finalize_run(session, run, findings)
                log.info(
                    "reconciliation_completed",
                    total_findings=run.finding_count,
                )

            except Exception:
                import traceback

                tb = traceback.format_exc()
                log.error("reconciliation_failed", error=tb)
                run.status = ReconciliationRunStatus.FAILED
                run.error_message = tb[:2048]
                run.completed_at = datetime.now(UTC)
                await session.flush()
                await session.commit()

        return run

    # ── Internal phases ─────────────────────────────────────────────

    @staticmethod
    async def _detect_duplicates(
        session: AsyncSession,
        run: ReconciliationRun,
        account_ids: list[str] | None,
        date_from: datetime,
        date_to: datetime,
        threshold_hours: int,
        provider_keys: list[str] | None = None,
    ) -> list[ReconciliationResult]:
        """Find potential duplicate transactions."""
        from finance_sync.db.uow import UnitOfWork

        findings: list[ReconciliationResult] = []

        async with UnitOfWork(session) as uow:
            pairs = await uow.transactions.find_duplicate_candidates(
                tenant_id=run.tenant_id,
                account_ids=account_ids,
                provider_keys=provider_keys,
                date_from=date_from,
                date_to=date_to,
                threshold_hours=threshold_hours,
            )

            for tx_a, tx_b in pairs:
                # Keep this guard here as well as in the repository so a
                # candidate from another implementation cannot become a
                # misleading Potential duplicate finding.
                if has_distinct_transaction_ids_in_descriptions(tx_a, tx_b):
                    continue
                # Derive a simple confidence score
                same_provider = tx_a.provider_key == tx_b.provider_key
                same_desc = (
                    tx_a.description
                    and tx_b.description
                    and tx_a.description.lower() == tx_b.description.lower()
                )
                confidence = 0.5
                if same_provider and not same_desc:
                    confidence = 0.6  # Same provider, different IDs
                elif not same_provider:
                    confidence = 0.7  # Cross-provider duplicate
                if same_desc:
                    confidence += 0.2

                severity = (
                    ReconciliationSeverity.ERROR
                    if confidence > 0.8
                    else ReconciliationSeverity.WARNING
                )

                amount_diff = abs(
                    (tx_a.amount or Decimal(0)) - (tx_b.amount or Decimal(0))
                )

                findings.append(
                    ReconciliationResult(
                        run_id=str(run.id),
                        tenant_id=run.tenant_id,
                        kind=ReconciliationResultKind.DUPLICATE_TRANSACTION,
                        severity=severity,
                        account_id=str(tx_a.account_id),
                        provider_key=tx_a.provider_key,
                        other_provider_key=tx_b.provider_key,
                        transaction_id_a=str(tx_a.id),
                        transaction_id_b=str(tx_b.id),
                        external_transaction_id_a=tx_a.external_transaction_id,
                        external_transaction_id_b=tx_b.external_transaction_id,
                        amount=tx_a.amount,
                        occurred_at=tx_a.occurred_at,
                        description=(
                            f"Potential duplicate: "
                            f"{tx_a.description or 'no desc'} / "
                            f"{tx_b.description or 'no desc'} "
                            f"({tx_a.provider_key} vs {tx_b.provider_key})"
                        ),
                        details={
                            "confidence": round(confidence, 2),
                            "amount_diff": str(amount_diff),
                            "diff_hours": round(
                                abs(
                                    (
                                        (tx_a.occurred_at or datetime.now(UTC))
                                        - (
                                            tx_b.occurred_at
                                            or datetime.now(UTC)
                                        )
                                    ).total_seconds()
                                )
                                / 3600,
                                1,
                            ),
                            "same_description": same_desc,
                            "same_provider": same_provider,
                        },
                    )
                )

        return findings

    @staticmethod
    async def _detect_cross_connector_gaps(
        session: AsyncSession,
        run: ReconciliationRun,
        account_ids: list[str] | None,
        _date_from: datetime,
        _date_to: datetime,
        provider_keys: list[str] | None = None,
    ) -> list[ReconciliationResult]:
        """Detect gaps between connector coverages for overlapping accounts.

        For accounts fed by multiple providers, compare the date ranges
        of each provider's transactions and flag any provider that has a
        significantly narrower range (possible missing data).  When
        ``provider_keys`` is given, only those providers are compared.
        """
        from sqlalchemy import select

        from finance_sync.models import Account

        findings: list[ReconciliationResult] = []

        # Get accounts to inspect
        conditions = [Account.tenant_id == run.tenant_id]  # type: ignore[attr-defined]
        if account_ids:
            conditions.append(Account.id.in_(account_ids))  # type: ignore[attr-defined]
        stmt = select(Account).where(*conditions)
        result = await session.execute(stmt)
        accounts: list[Account] = list(result.scalars().all())

        for acct in accounts:
            # Count providers for this account
            from finance_sync.db.uow import UnitOfWork

            async with UnitOfWork(session) as uow:
                providers = await uow.transactions.get_providers_for_account(
                    run.tenant_id, str(acct.id)
                )

            if len(providers) < 2:
                continue  # Single-provider account — cross-connector N/A

            # If specific provider_keys requested, intersect with available
            if provider_keys is not None:
                providers = [p for p in providers if p in provider_keys]
                if len(providers) < 2:
                    continue  # Fewer than 2 requested providers

            # For each provider, get its transaction date range
            provider_ranges: dict[
                str, tuple[datetime | None, datetime | None]
            ] = {}
            async with UnitOfWork(session) as uow:
                for p in providers:
                    provider_ranges[
                        p
                    ] = await uow.transactions.get_transaction_date_range(
                        run.tenant_id, account_id=str(acct.id), provider_key=p
                    )

            # Find the overall range
            all_starts = [r[0] for r in provider_ranges.values() if r[0]]
            all_ends = [r[1] for r in provider_ranges.values() if r[1]]
            if not all_starts or not all_ends:
                continue

            overall_start = min(all_starts)
            overall_end = max(all_ends)

            for provider_key, (p_start, p_end) in provider_ranges.items():
                if p_start is None or p_end is None:
                    no_txn_msg = (
                        f"Connector '{provider_key}' has no transactions "
                        f"for account '{acct.name}' while others do"
                    )
                    findings.append(
                        ReconciliationResult(
                            run_id=str(run.id),
                            tenant_id=run.tenant_id,
                            kind=ReconciliationResultKind.MISSING_TRANSACTION,
                            severity=ReconciliationSeverity.ERROR,
                            account_id=str(acct.id),
                            provider_key=provider_key,
                            description=no_txn_msg,
                            details={
                                "account_name": acct.name,
                                "overall_start": overall_start.isoformat(),
                                "overall_end": overall_end.isoformat(),
                                "all_providers": providers,
                            },
                        )
                    )
                    continue

                # Check if provider's coverage starts significantly later
                start_diff = (p_start - overall_start).total_seconds()
                if start_diff > 86400 * 7:  # More than 7 days late
                    findings.append(
                        ReconciliationResult(
                            run_id=str(run.id),
                            tenant_id=run.tenant_id,
                            kind=ReconciliationResultKind.MISSING_TRANSACTION,
                            severity=ReconciliationSeverity.WARNING,
                            account_id=str(acct.id),
                            provider_key=provider_key,
                            description=(
                                f"Connector '{provider_key}' started recording "
                                f"transactions for '{acct.name}' "
                                f"{start_diff / 86400:.0f} days after the "
                                f"earliest provider"
                            ),
                            details={
                                "account_name": acct.name,
                                "provider_start": p_start.isoformat(),
                                "overall_start": overall_start.isoformat(),
                                "gap_days": round(start_diff / 86400, 1),
                                "all_providers": providers,
                            },
                        )
                    )

        return findings

    @staticmethod
    async def _detect_missing_transactions(
        session: AsyncSession,
        run: ReconciliationRun,
        account_ids: list[str] | None,
        _date_from: datetime,
        _date_to: datetime,
        provider_keys: list[str] | None = None,
    ) -> list[ReconciliationResult]:
        """Detect potential missing transaction windows.

        Looks for accounts where a connector has a gap in its transaction
        date range relative to the requested analysis period — no data
        at all from a connector for a period where data was expected.
        When ``provider_keys`` is given, only those providers are checked.
        """
        from sqlalchemy import select

        from finance_sync.models import Account

        findings: list[ReconciliationResult] = []

        # Build scope filter
        conditions = [Account.tenant_id == run.tenant_id]  # type: ignore[attr-defined]
        if account_ids:
            conditions.append(Account.id.in_(account_ids))  # type: ignore[attr-defined]
        stmt = select(Account).where(*conditions)
        result = await session.execute(stmt)
        accounts: list[Account] = list(result.scalars().all())

        for acct in accounts:
            from finance_sync.db.uow import UnitOfWork

            async with UnitOfWork(session) as uow:
                providers = await uow.transactions.get_providers_for_account(
                    run.tenant_id, str(acct.id)
                )

            for provider_key in providers:
                # If specific provider_keys requested, skip non-matching
                if (
                    provider_keys is not None
                    and provider_key not in provider_keys
                ):
                    continue
                async with UnitOfWork(session) as uow:
                    (
                        p_start,
                        _p_end,
                    ) = await uow.transactions.get_transaction_date_range(
                        run.tenant_id,
                        account_id=str(acct.id),
                        provider_key=provider_key,
                    )

                if p_start is None:
                    continue  # Already flagged in gap detection

                # Check if the provider's coverage extends to analysis boundary
                if p_start > _date_from:
                    gap_days = (p_start - _date_from).total_seconds() / 86400
                    if gap_days > 7:
                        findings.append(
                            ReconciliationResult(
                                run_id=str(run.id),
                                tenant_id=run.tenant_id,
                                kind=ReconciliationResultKind.MISSING_TRANSACTION,
                                severity=ReconciliationSeverity.INFO,
                                account_id=str(acct.id),
                                provider_key=provider_key,
                                description=(
                                    f"Connector '{provider_key}' has a "
                                    f"{gap_days:.0f}-day gap at the "
                                    f"start of the analysis window "
                                    f"for '{acct.name}'"
                                ),
                                details={
                                    "account_name": acct.name,
                                    "provider_start": p_start.isoformat(),
                                    "analysis_start": _date_from.isoformat(),
                                    "gap_days": round(gap_days, 1),
                                },
                            )
                        )

        return findings

    # ── Finalization ─────────────────────────────────────────────────

    @staticmethod
    async def _finalize_run(
        session: AsyncSession,
        run: ReconciliationRun,
        findings: list[ReconciliationResult],
    ) -> None:
        """Persist all findings and finalize the run."""
        for f in findings:
            session.add(f)

        await session.flush()

        # Detection only registers durable work.  It never constructs a
        # connector or performs provider I/O.
        from finance_sync.connectors.registry import ConnectorRegistry
        from finance_sync.models import Account
        from finance_sync.reconciliation.remediation.backlog import (
            DetectedIssue,
        )
        from finance_sync.services.remediation import RemediationService

        account_ids = {
            str(finding.account_id)
            for finding in findings
            if isinstance(finding.account_id, str)
            and finding.account_id
            and (
                finding.kind.value
                if hasattr(finding.kind, "value")
                else str(finding.kind)
            )
            == "missing_transaction"
        }
        account_by_id: dict[str, Account] = {}
        if account_ids:
            account_rows = await session.execute(
                select(Account).where(Account.id.in_(account_ids))
            )
            account_by_id = {
                str(account.id): account for account in account_rows.scalars()
            }
        connector_catalog = ConnectorRegistry().list_connectors()

        issues: list[DetectedIssue] = []
        for finding in findings:
            entity_ids = sorted(
                value
                for value in (
                    finding.transaction_id_a,
                    finding.transaction_id_b,
                )
                if isinstance(value, str) and value
            )
            account_id = (
                finding.account_id
                if isinstance(finding.account_id, str)
                else None
            )
            strategy = "unsupported"
            connection_id: str | None = None
            context: dict[str, Any] = {
                "account_id": account_id,
                "details": finding.details
                if isinstance(finding.details, dict)
                else {},
            }
            finding_kind = (
                finding.kind.value
                if hasattr(finding.kind, "value")
                else str(finding.kind)
            )
            if finding_kind == "missing_transaction" and account_id is not None:
                account = account_by_id.get(account_id)
                provider = (
                    str(finding.provider_key).lower()
                    if isinstance(finding.provider_key, str)
                    else ""
                )
                metadata = connector_catalog.get(provider, {})
                declared_value = metadata.get("remediation_strategies", [])
                declared = (
                    cast("list[object]", declared_value)
                    if isinstance(declared_value, list)
                    else []
                )
                supports_history = any(
                    isinstance(entry, dict)
                    and cast("dict[str, Any]", entry).get("key")
                    == "transaction_history_gap"
                    for entry in declared
                )
                details = (
                    finding.details if isinstance(finding.details, dict) else {}
                )
                run_scope = run.scope if isinstance(run.scope, dict) else {}
                window_start = (
                    details.get("analysis_start")
                    or details.get("overall_start")
                    or run_scope.get("date_from")
                )
                if isinstance(window_start, datetime):
                    window_start = window_start.astimezone(UTC).isoformat()
                window_end = details.get("overall_end") or run_scope.get(
                    "date_to"
                )
                if isinstance(window_end, datetime):
                    window_end = window_end.astimezone(UTC).isoformat()
                if (
                    account is not None
                    and account.connection_id is not None
                    and supports_history
                    and isinstance(window_start, str)
                    and isinstance(window_end, str)
                ):
                    strategy = "transaction_history_gap"
                    connection_id = str(account.connection_id)
                    context = {
                        "account_id": account_id,
                        "provider_account_id": account.external_account_id,
                        "from": window_start,
                        "to": window_end,
                        "minimum_transactions": 1,
                        "limit": 500,
                    }
            finding_id = finding.id
            entity_id = ":".join(entity_ids) or account_id or finding_id
            issues.append(
                DetectedIssue(
                    tenant_id=str(run.tenant_id),
                    provider_key=finding.provider_key
                    if isinstance(finding.provider_key, str)
                    else "unknown",
                    issue_type=finding.kind.value
                    if hasattr(finding.kind, "value")
                    else str(finding.kind),
                    affected_entity_type="transaction_pair"
                    if len(entity_ids) > 1
                    else "account",
                    affected_entity_id=entity_id,
                    severity=finding.severity.value
                    if hasattr(finding.severity, "value")
                    else str(finding.severity),
                    remediation_strategy=strategy,
                    connection_id=connection_id,
                    context=context,
                    scope=str(finding.details)
                    if isinstance(finding.details, dict)
                    else entity_id,
                    detected_at=datetime.now(UTC),
                )
            )
        if issues:
            remediation_items = await RemediationService(
                session, str(run.tenant_id)
            ).register_detected_issues(issues)
            for finding, item in zip(findings, remediation_items, strict=True):
                finding.remediation_item_id = str(item.id)
            await session.flush()

        # Compute summary stats
        by_kind: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for f in findings:
            by_kind[str(f.kind)] = by_kind.get(str(f.kind), 0) + 1
            by_severity[str(f.severity)] = (
                by_severity.get(str(f.severity), 0) + 1
            )

        run.finding_count = len(findings)
        run.summary = {
            "by_kind": by_kind,
            "by_severity": by_severity,
        }
        run.status = ReconciliationRunStatus.COMPLETED
        run.completed_at = datetime.now(UTC)
        await session.flush()
        await session.commit()

    # ── Query helpers ───────────────────────────────────────────────

    async def list_runs(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[ReconciliationRun]:
        """Return recent reconciliation runs for the tenant."""
        from sqlalchemy import desc, select

        stmt = (
            select(ReconciliationRun)
            .where(ReconciliationRun.tenant_id == self._tenant_id)  # type: ignore[attr-defined]
            .order_by(desc(ReconciliationRun.created_at))  # type: ignore[attr-defined]
            .offset(offset)
            .limit(limit)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_run_with_results(
        self,
        run_id: str,
        *,
        result_limit: int = 100,
        result_offset: int = 0,
        kind_filter: str | None = None,
        severity_filter: str | None = None,
        visible_account_ids: Any | None = None,
    ) -> tuple[ReconciliationRun | None, list[ReconciliationResult], int]:
        """Return a reconciliation run and its findings.

        When ``visible_account_ids`` is given (a SQL predicate such as
        ``scope.account_ids_subquery()``), only findings referencing
        one of those accounts are returned (account-scope scoping);
        findings outside the scope are hidden.
        """
        from sqlalchemy import desc, func, select

        async with self._session_factory() as session:
            run = await session.get(ReconciliationRun, run_id)
            if run is None:
                return (None, [], 0)

            conditions = [ReconciliationResult.run_id == run_id]  # type: ignore[attr-defined]
            if visible_account_ids is not None:
                conditions.append(  # type: ignore[attr-defined]
                    ReconciliationResult.account_id.in_(visible_account_ids)
                )
            if kind_filter:
                conditions.append(ReconciliationResult.kind == kind_filter)  # type: ignore[attr-defined]
            if severity_filter:
                conditions.append(  # type: ignore[attr-defined]
                    ReconciliationResult.severity == severity_filter
                )

            # Count
            count_stmt = (
                select(func.count())
                .select_from(ReconciliationResult)
                .where(*conditions)
            )
            total_result = await session.execute(count_stmt)
            total: int = total_result.scalar() or 0  # type: ignore[assignment]

            # Fetch
            stmt = (
                select(ReconciliationResult)
                .where(*conditions)
                .order_by(desc(ReconciliationResult.created_at))  # type: ignore[attr-defined]
                .offset(result_offset)
                .limit(result_limit)
            )
            result = await session.execute(stmt)
            results: list[ReconciliationResult] = list(result.scalars().all())

            return (run, results, total)


# ── Helpers ───────────────────────────────────────────────────────────


def _default_since(now: datetime | None = None) -> datetime:
    """Return a default look-back datetime (90 days ago)."""
    from datetime import timedelta

    return (now or datetime.now(UTC)) - timedelta(days=_DEFAULT_DAYS_BACK)
