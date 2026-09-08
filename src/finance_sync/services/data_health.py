"""Canonical aggregation for the user-facing Data health workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, case, exists, func, or_, select

from finance_sync.exporter.models import ExportRun
from finance_sync.exporter.wealthfolio.models import (
    WealthfolioAccountMapping,
    WealthfolioDelivery,
)
from finance_sync.models import (
    Account,
    Balance,
    Credential,
    EnrichmentFreshness,
    ExportTarget,
    Holding,
    ImportRun,
    Security,
    SecurityListing,
    SyncCursor,
    SyncRun,
    TaxLot,
    Transaction,
)
from finance_sync.schemas.data_health import (
    DataHealthCategory,
    DataHealthIssue,
    DataHealthOverview,
    DataHealthReconciliation,
    DataHealthSource,
    DataHealthStatus,
)
from finance_sync.services.control_plane import ControlPlaneService
from finance_sync.services.control_plane_actions import action
from finance_sync.services.data_quality import DataQualityService
from finance_sync.services.wealthfolio_preflight import (
    quantity_event_ratio,
    validate_activity_semantics,
    validate_transaction_stream,
    validate_transfer_rows,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.schemas.control_plane import ControlPlaneIssue


def _finite_decimal(value: object) -> Decimal | None:
    """Parse persisted numeric values without allowing NaN/Infinity through."""
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


class DataHealthService:
    """Compose existing operational projections into one actionable contract."""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: str,
        *,
        permissions: set[str] | None = None,
        redis_configured: bool = False,
        now: datetime | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._permissions = permissions
        self._redis_configured = redis_configured
        self._now = now or datetime.now(UTC)

    async def get_overview(self) -> DataHealthOverview:
        control = await ControlPlaneService(
            self._session,
            self._tenant_id,
            permissions=self._permissions,
            redis_configured=self._redis_configured,
            now=self._now,
        ).get_overview()
        quality = await DataQualityService(
            self._session, self._tenant_id, now=self._now
        ).get_overview()

        issues = [self._issue(issue) for issue in control.issues]
        sources = [
            DataHealthSource(
                id=connection.id,
                provider=connection.provider,
                status=connection.status,
                last_success_at=connection.last_success_at,
                last_attempt_at=connection.last_attempt_at,
                accounts=next(
                    (
                        coverage.accounts
                        for coverage in quality.coverage
                        if coverage.provider == connection.provider
                    ),
                    0,
                ),
                transactions=next(
                    (
                        coverage.transactions
                        for coverage in quality.coverage
                        if coverage.provider == connection.provider
                    ),
                    0,
                ),
            )
            for connection in control.connections
        ]
        issues.extend(self._missing_source_issues(sources, control))
        issues.extend(await self._changed_provider_issues(sources))
        issues.extend(await self._additional_issues())
        # A few lightweight callers intentionally construct this composer
        # without a database session; preserve the projection-only mode.
        if getattr(self, "_session", None) is not None:
            issues.extend(await self._account_identity_issues())
            issues.extend(await self._account_metadata_identity_issues())
            issues.extend(await self._orphaned_account_issues())
            issues.extend(await self._selected_account_issues())
            issues.extend(await self._transaction_identity_issues())
            issues.extend(await self._transaction_fingerprint_issues())
            issues.extend(await self._transaction_semantic_duplicate_issues())
            issues.extend(await self._transaction_relationship_issues())
            issues.extend(await self._sync_integrity_issues())
            issues.extend(await self._portfolio_quantity_issues())
            issues.extend(await self._cash_reconciliation_issues())
            issues.extend(await self._tax_lot_integrity_issues())
            issues.extend(await self._security_identity_issues())
            issues.extend(await self._destination_parity_issues())
            issues.extend(await self._tombstoned_export_issues())
            issues.extend(await self._canonical_data_issues())
            issues.extend(await self._wealthfolio_preflight_issues())
        severity_order = {"error": 0, "warning": 1, "info": 2}
        issues.sort(
            key=lambda issue: (
                severity_order.get(issue.severity, 99),
                issue.category,
                issue.id,
            )
        )
        stale_data = {
            "securities_stale": control.freshness.securities_stale,
            "securities_without_quote": (
                control.freshness.securities_without_quote
            ),
            "holdings_without_valuation": (
                control.freshness.holdings_without_valuation
            ),
        }
        return DataHealthOverview(
            status=self._status(control.status, quality.status, issues),
            last_successful_sync=max(
                (
                    source.last_success_at
                    for source in sources
                    if source.last_success_at
                ),
                default=None,
            ),
            sources=sources,
            stale_data=stale_data,
            unresolved_securities=sum(
                1 for issue in issues if issue.category == "unresolved_security"
            ),
            failed_exports=sum(
                1
                for issue in issues
                if issue.category in {"export", "destination", "failed_export"}
            ),
            reconciliation=DataHealthReconciliation(
                findings_total=quality.findings_total,
                findings_by_kind=quality.findings_by_kind,
                latest_run_at=quality.latest_run_at,
            ),
            issues=issues,
            as_of=control.as_of,
            generated_at=self._now,
        )

    async def _tombstoned_export_issues(self) -> list[DataHealthIssue]:
        """Find tombstoned transactions already inside a delivery scope."""
        rows = (
            await self._session.execute(
                select(
                    Transaction.id,
                    Transaction.account_id,
                    Transaction.occurred_at,
                    Transaction.tombstoned_at,
                    WealthfolioDelivery.target_id,
                    WealthfolioDelivery.last_exported_at,
                )
                .join(
                    WealthfolioDelivery,
                    and_(
                        WealthfolioDelivery.tenant_id == self._tenant_id,
                        WealthfolioDelivery.account_id
                        == Transaction.account_id,
                    ),
                )
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.tombstoned_at.is_not(None),
                    WealthfolioDelivery.last_exported_at.is_not(None),
                    Transaction.occurred_at
                    <= WealthfolioDelivery.last_exported_at,
                )
                .order_by(
                    WealthfolioDelivery.target_id,
                    Transaction.tombstoned_at,
                )
            )
        ).all()
        if not rows:
            return []
        by_target: dict[str, list[tuple[object, ...]]] = {}
        for row in rows:
            values = tuple(row)
            by_target.setdefault(str(values[4]), []).append(values)

        issues: list[DataHealthIssue] = []
        for target_id, target_rows in by_target.items():
            transaction_ids = [str(row[0]) for row in target_rows]
            digest = self._stable_issue_hash(
                f"{target_id}:" + "|".join(sorted(transaction_ids))
            )
            target_path = (
                "/api/v1/destinations"
                if target_id == "legacy"
                else f"/api/v1/destinations/{target_id}/test"
            )
            issues.append(
                DataHealthIssue(
                    id=f"tombstoned-export:{target_id}:{digest}",
                    category="destination_drift",
                    severity="warning",
                    title="Getombstonede transactie valt binnen exportscope",
                    description=(
                        "Een lokaal ingetrokken transactie is eerder naar de "
                        "Wealthfolio-deliveryscope gestuurd. Test de target "
                        "om stale remote activiteit te bevestigen; Data "
                        "health verwijdert remote data niet automatisch."
                    ),
                    impact_count=len(transaction_ids),
                    affected_record_count=len(transaction_ids),
                    source="transactions_and_wealthfolio_deliveries",
                    affected_transaction_ids=transaction_ids[:100],
                    evidence={
                        "target_id": target_id,
                        "total_count": len(transaction_ids),
                        "detail_limit": 100,
                        "remote_verification_required": True,
                    },
                    blocking=False,
                    action=action(
                        "test_destination",
                        target_path,
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    async def _account_identity_issues(self) -> list[DataHealthIssue]:
        """Find provider accounts that cannot be treated as one identity."""
        rows = list(
            (
                await self._session.execute(
                    select(Account).where(
                        Account.tenant_id == self._tenant_id,
                        Account.is_active.is_(True),
                    )
                )
            ).scalars()
        )
        by_provider: dict[str, list[Account]] = {}
        for row in rows:
            by_provider.setdefault(str(row.provider_key), []).append(row)

        issues: list[DataHealthIssue] = []
        for provider, accounts in by_provider.items():
            # Trading212's API connector exposes exactly one account per API
            # key. Multiple active rows on one connection are therefore a
            # deterministic identity conflict, not a valid multi-account
            # selection.
            if provider == "trading212":
                by_connection: dict[str, list[Account]] = {}
                for account in accounts:
                    key = str(account.connection_id or "legacy")
                    by_connection.setdefault(key, []).append(account)
                for connection_id, candidates in by_connection.items():
                    if len(candidates) < 2:
                        continue
                    issues.append(
                        self._account_identity_issue(
                            provider,
                            connection_id,
                            candidates,
                            blocking=True,
                        )
                    )

                legacy_rows = [
                    account
                    for account in accounts
                    if str(account.external_account_id) == "trading212"
                ]
                if legacy_rows and len(accounts) > len(legacy_rows):
                    issues.append(
                        self._account_identity_issue(
                            provider,
                            "trading212-legacy-fallback",
                            accounts,
                            blocking=True,
                        )
                    )

            # Exact provider identities must not be duplicated across
            # connections or legacy rows. This also catches historical rows
            # with NULL connection_id after a multi-connection migration.
            by_external_id: dict[str, list[Account]] = {}
            for account in accounts:
                by_external_id.setdefault(
                    str(account.external_account_id), []
                ).append(account)
            for external_id, candidates in by_external_id.items():
                connection_ids = {
                    str(account.connection_id or "legacy")
                    for account in candidates
                }
                if len(candidates) > 1 and len(connection_ids) > 1:
                    issues.append(
                        self._account_identity_issue(
                            provider,
                            external_id,
                            candidates,
                            blocking=False,
                        )
                    )

        return issues

    async def _orphaned_account_issues(self) -> list[DataHealthIssue]:
        """Find active accounts detached from their configured connection."""
        accounts = list(
            (
                await self._session.execute(
                    select(Account).where(
                        Account.tenant_id == self._tenant_id,
                        Account.is_active.is_(True),
                        Account.connection_id.is_not(None),
                    )
                )
            ).scalars()
        )
        if not accounts:
            return []
        credentials = list(
            (
                await self._session.execute(
                    select(Credential).where(
                        Credential.tenant_id == self._tenant_id,
                    )
                )
            ).scalars()
        )
        by_id = {str(credential.id): credential for credential in credentials}
        grouped: dict[tuple[str, str], list[Account]] = {}
        reasons: dict[tuple[str, str], str] = {}
        for account in accounts:
            connection_id = str(account.connection_id)
            credential = by_id.get(connection_id)
            if credential is None:
                key = (connection_id, str(account.provider_key))
                reasons[key] = "missing_connection"
            elif str(credential.provider_key) != str(account.provider_key):
                key = (connection_id, str(account.provider_key))
                reasons[key] = "provider_mismatch"
            else:
                continue
            grouped.setdefault(key, []).append(account)

        issues: list[DataHealthIssue] = []
        for (connection_id, provider), candidates in grouped.items():
            account_ids = sorted({str(account.id) for account in candidates})
            reason = reasons[(connection_id, provider)]
            digest = self._stable_issue_hash(
                f"{provider}:{connection_id}:{reason}:" + "|".join(account_ids)
            )
            issues.append(
                DataHealthIssue(
                    id=f"orphaned-account-connection:{provider}:{digest}",
                    category="account_identity_conflict",
                    severity="warning",
                    title="Account verwijst naar ontbrekende connector",
                    description=(
                        "Een actieve lokale account verwijst naar een "
                        "connector "
                        "die niet meer bestaat of niet bij dezelfde provider "
                        "hoort. Controleer de connector- en accountrelatie "
                        "voordat je synchroniseert of exporteert."
                    ),
                    impact_count=len(account_ids),
                    affected_record_count=len(account_ids),
                    provider=provider,
                    connection_id=connection_id,
                    account_ids=account_ids,
                    source="accounts_and_credentials",
                    evidence={
                        "reason": reason,
                        "total_count": len(account_ids),
                        "detail_limit": 100,
                    },
                    blocking=False,
                    action=action(
                        "view_accounts",
                        "/api/v1/accounts",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    async def _selected_account_issues(self) -> list[DataHealthIssue]:
        """Find selected provider accounts missing from the local dataset.

        Only connection metadata and account identities are read.  The
        encrypted credential payload is deliberately never loaded or
        decrypted by Data health.
        """
        credentials = list(
            (
                await self._session.execute(
                    select(Credential).where(
                        Credential.tenant_id == self._tenant_id,
                        Credential.status == "active",
                        Credential.selected_accounts.is_not(None),
                    )
                )
            ).scalars()
        )
        if not credentials:
            return []

        accounts = list(
            (
                await self._session.execute(
                    select(Account).where(
                        Account.tenant_id == self._tenant_id,
                        Account.is_active.is_(True),
                    )
                )
            ).scalars()
        )
        available: dict[tuple[str, str], set[str]] = {}
        for account in accounts:
            if account.connection_id is None:
                continue
            available.setdefault(
                (str(account.connection_id), str(account.provider_key)),
                set(),
            ).add(str(account.external_account_id))

        issues: list[DataHealthIssue] = []
        for credential in credentials:
            selected = {
                str(value).strip()
                for value in (credential.selected_accounts or [])
                if str(value).strip()
            }
            if not selected:
                continue
            connection_id = str(credential.id)
            present = available.get(
                (connection_id, str(credential.provider_key)), set()
            )
            missing = sorted(selected - present)
            if not missing:
                continue
            digest = self._stable_issue_hash(
                f"{credential.provider_key}:{connection_id}:"
                + "|".join(missing)
            )
            issues.append(
                DataHealthIssue(
                    id=f"selected-account-missing:{credential.provider_key}:{digest}",
                    category="account_identity_conflict",
                    severity="warning",
                    title="Geselecteerde provider-account ontbreekt lokaal",
                    description=(
                        "Een actieve connectorselectie verwijst naar een of "
                        "meer provider-accounts die niet in de laatste "
                        "lokale accountdataset aanwezig zijn. Controleer de "
                        "accountselectie en voer daarna een sync uit."
                    ),
                    impact_count=len(missing),
                    affected_record_count=len(missing),
                    provider=str(credential.provider_key),
                    connection_id=connection_id,
                    source="credentials_and_accounts",
                    evidence={
                        "selected_count": len(selected),
                        "missing_count": len(missing),
                        "missing_external_account_ids": missing[:100],
                        "detail_limit": 100,
                    },
                    blocking=False,
                    action=action(
                        "view_connection",
                        f"/api/v1/connectors/configs/{connection_id}",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    async def _account_metadata_identity_issues(
        self,
    ) -> list[DataHealthIssue]:
        """Find stable provider metadata reused by distinct account IDs.

        Only a narrow allowlist of identity-shaped metadata keys is eligible;
        arbitrary provider payloads are never hashed into a user-visible
        detail or returned as evidence.
        """
        rows = list(
            (
                await self._session.execute(
                    select(Account).where(
                        Account.tenant_id == self._tenant_id,
                        Account.is_active.is_(True),
                    )
                )
            ).scalars()
        )
        groups: dict[tuple[str, str, str], list[Account]] = {}
        metadata_aliases = (
            ("iban", ("iban",)),
            ("account_number", ("account_number", "accountNumber")),
            (
                "account_id",
                (
                    "account_id",
                    "accountId",
                    "accountID",
                    "client_account_id",
                    "clientAccountId",
                    "brokerage_account_id",
                    "brokerageAccountId",
                ),
            ),
            (
                "account_reference",
                ("account_reference", "accountReference"),
            ),
            (
                "provider_account_id",
                ("provider_account_id", "providerAccountId"),
            ),
            (
                "monetary_account_id",
                ("monetary_account_id", "monetaryAccountId"),
            ),
            (
                "bank_account_id",
                ("bank_account_id", "bankAccountId"),
            ),
            (
                "portfolio_id",
                ("portfolio_id", "portfolioId"),
            ),
        )
        for account in rows:
            metadata = account.provider_metadata
            if not isinstance(metadata, dict):
                continue
            for identity_key, aliases in metadata_aliases:
                for key in aliases:
                    value = metadata.get(key)
                    if not isinstance(value, (str, int)):
                        continue
                    normalized = "".join(
                        character
                        for character in str(value).upper()
                        if character.isalnum()
                    )
                    if len(normalized) < 6:
                        continue
                    groups.setdefault(
                        (str(account.provider_key), identity_key, normalized),
                        [],
                    ).append(account)

        issues: list[DataHealthIssue] = []
        for (provider, metadata_key, _normalized), accounts in groups.items():
            unique_accounts = {str(account.id): account for account in accounts}
            distinct_external_ids = {
                str(account.external_account_id)
                for account in unique_accounts.values()
            }
            if len(distinct_external_ids) < 2:
                continue
            account_ids = sorted(unique_accounts)
            identity_hash = self._stable_issue_hash(
                f"{provider}:{metadata_key}:{_normalized}"
            )
            issues.append(
                DataHealthIssue(
                    id=f"account-metadata-identity:{provider}:{identity_hash}",
                    category="account_identity_conflict",
                    severity="warning",
                    title="Providermetadata wijst op dubbele accounts",
                    description=(
                        "Meerdere canonical accounts delen dezelfde stabiele "
                        "provider-identiteit in metadata, maar hebben een "
                        "verschillende external account-ID. Controleer de "
                        "connector voordat je exporteert."
                    ),
                    impact_count=len(account_ids),
                    affected_record_count=len(account_ids),
                    provider=provider,
                    account_ids=account_ids,
                    source="accounts",
                    evidence={
                        "metadata_key": metadata_key,
                        "identity_hash": identity_hash,
                    },
                    blocking=False,
                    action=action(
                        "view_accounts",
                        "/api/v1/accounts",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    async def _transaction_identity_issues(self) -> list[DataHealthIssue]:
        """Find provider transaction IDs reused across local connections."""
        rows = (
            await self._session.execute(
                select(
                    Transaction.provider_key,
                    Transaction.external_transaction_id,
                    func.count(Transaction.id),
                )
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.external_transaction_id.is_not(None),
                )
                .group_by(
                    Transaction.provider_key,
                    Transaction.external_transaction_id,
                )
                .having(func.count(Transaction.id) > 1)
                .order_by(Transaction.provider_key)
            )
        ).all()
        issues: list[DataHealthIssue] = []
        for provider, external_id, count in rows:
            digest = self._stable_issue_hash(f"{provider}:{external_id}")
            issues.append(
                DataHealthIssue(
                    id=f"transaction-identity:{provider}:{digest}",
                    category="duplicate_transaction_identity",
                    severity="warning",
                    title="Providertransactie komt meerdere keren voor",
                    description=(
                        f"Providertransactie {external_id} van {provider} "
                        "komt meerdere keren voor, mogelijk over meerdere "
                        "verbindingen of legacy records."
                    ),
                    impact_count=int(count),
                    affected_record_count=int(count),
                    provider=str(provider),
                    source="transactions",
                    evidence={
                        "external_transaction_id": str(external_id),
                        "total_count": int(count),
                        "detail_limit": 0,
                    },
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    async def _transaction_fingerprint_issues(self) -> list[DataHealthIssue]:
        """Find provider fingerprints reused by multiple transactions."""
        rows = (
            await self._session.execute(
                select(
                    Transaction.provider_key,
                    Transaction.provider_fingerprint,
                    func.count(Transaction.id),
                )
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.provider_fingerprint.is_not(None),
                )
                .group_by(
                    Transaction.provider_key,
                    Transaction.provider_fingerprint,
                )
                .having(func.count(Transaction.id) > 1)
                .order_by(Transaction.provider_key)
            )
        ).all()
        issues: list[DataHealthIssue] = []
        for provider, fingerprint, count in rows:
            digest = self._stable_issue_hash(f"{provider}:{fingerprint}")
            issues.append(
                DataHealthIssue(
                    id=f"transaction-fingerprint:{provider}:{digest}",
                    category="duplicate_transaction_identity",
                    severity="warning",
                    title="Providerfingerprint komt meerdere keren voor",
                    description=(
                        f"{count} transacties van {provider} delen dezelfde "
                        "providerfingerprint. Controleer of dit een echte "
                        "duplicatie of een provider-revisie is."
                    ),
                    impact_count=int(count),
                    affected_record_count=int(count),
                    provider=str(provider),
                    source="transactions",
                    # The fingerprint itself can be sensitive provider data;
                    # expose only a stable redacted digest.
                    evidence={
                        "fingerprint_hash": digest,
                        "total_count": int(count),
                        "detail_limit": 0,
                    },
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    async def _transaction_semantic_duplicate_issues(
        self,
    ) -> list[DataHealthIssue]:
        """Find likely duplicates when a provider supplies no transaction ID."""
        transaction_date = func.date(Transaction.occurred_at).label(
            "transaction_date"
        )
        rows = (
            await self._session.execute(
                select(
                    Transaction.provider_key,
                    Transaction.account_id,
                    transaction_date,
                    Transaction.transaction_type,
                    Transaction.amount,
                    Transaction.currency_code,
                    Transaction.quantity,
                    Transaction.security_id,
                    func.count(Transaction.id),
                )
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    or_(
                        Transaction.external_transaction_id.is_(None),
                        func.trim(Transaction.external_transaction_id) == "",
                    ),
                )
                .group_by(
                    Transaction.provider_key,
                    Transaction.account_id,
                    transaction_date,
                    Transaction.transaction_type,
                    Transaction.amount,
                    Transaction.currency_code,
                    Transaction.quantity,
                    Transaction.security_id,
                )
                .having(func.count(Transaction.id) > 1)
                .order_by(Transaction.provider_key, transaction_date)
            )
        ).all()
        issues: list[DataHealthIssue] = []
        for (
            provider,
            account_id,
            occurred_on,
            transaction_type,
            amount,
            currency,
            quantity,
            security_id,
            count,
        ) in rows:
            semantic_key = ":".join(
                str(value or "")
                for value in (
                    provider,
                    account_id,
                    occurred_on,
                    transaction_type,
                    amount,
                    currency,
                    quantity,
                    security_id,
                )
            )
            digest = self._stable_issue_hash(semantic_key)
            issues.append(
                DataHealthIssue(
                    id=f"transaction-semantic-duplicate:{provider}:{digest}",
                    category="duplicate_transaction_identity",
                    severity="warning",
                    title="Mogelijke transactieduplicatie zonder provider-ID",
                    description=(
                        "Meerdere transacties zonder bruikbare provider-ID "
                        "delen dezelfde semantische sleutel. Controleer of "
                        "dit dubbele bronrecords of een legitieme herhaling is."
                    ),
                    impact_count=int(count),
                    affected_record_count=int(count),
                    provider=str(provider),
                    account_ids=[str(account_id)],
                    source="transactions",
                    evidence={
                        "semantic_key_hash": digest,
                        "transaction_date": str(occurred_on),
                        "security_id_present": security_id is not None,
                        "total_count": int(count),
                        "detail_limit": 0,
                    },
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    async def _transaction_relationship_issues(self) -> list[DataHealthIssue]:
        """Find transactions detached from their account/connector scope."""
        rows = (
            await self._session.execute(
                select(
                    Transaction.id,
                    Transaction.provider_key,
                    Transaction.connection_id,
                    Account.id,
                    Account.provider_key,
                    Account.connection_id,
                )
                .outerjoin(Account, Account.id == Transaction.account_id)
                .where(Transaction.tenant_id == self._tenant_id)
            )
        ).all()
        broken: list[tuple[object, ...]] = []
        for row in rows:
            (
                _transaction_id,
                provider,
                connection_id,
                account_id,
                account_provider,
                account_connection_id,
            ) = row
            if account_id is None or account_provider != provider:
                broken.append(tuple(row))
                continue
            if connection_id != account_connection_id:
                broken.append(tuple(row))
        if not broken:
            return []
        digest = self._stable_issue_hash(
            ":".join(sorted(str(row[0]) for row in broken))
        )
        return [
            DataHealthIssue(
                id=f"transaction-relationship:{digest}",
                category="account_identity_conflict",
                severity="error",
                title="Transacties zijn verkeerd aan accounts gekoppeld",
                description=(
                    "Een of meer transacties verwijzen naar een ontbrekende "
                    "account, een andere provider of een andere connector."
                ),
                impact_count=len(broken),
                affected_record_count=len(broken),
                source="transactions",
                affected_transaction_ids=[str(row[0]) for row in broken[:100]],
                evidence={
                    "total_count": len(broken),
                    "detail_limit": 100,
                },
                details=[
                    (
                        f"transaction={row[0]} provider={row[1]} "
                        f"connection={row[2] or 'legacy'} "
                        f"account={row[3] or 'missing'} "
                        f"account_connection={row[5] or 'legacy'}"
                    )
                    for row in broken[:100]
                ],
                blocking=True,
                action=action(
                    "view_transactions",
                    "/api/v1/transactions",
                    permissions=self._permissions,
                ),
            )
        ]

    async def _sync_integrity_issues(self) -> list[DataHealthIssue]:
        """Detect orphaned cursors and completed runs with partial output."""
        cursor_rows = (
            await self._session.execute(
                select(SyncCursor, Credential)
                .outerjoin(
                    Credential,
                    and_(
                        Credential.id == SyncCursor.connection_id,
                        Credential.tenant_id == self._tenant_id,
                    ),
                )
                .where(SyncCursor.tenant_id == self._tenant_id)
            )
        ).all()
        cursor_issues: list[str] = []
        for cursor, credential in cursor_rows:
            if credential is None:
                cursor_issues.append(
                    f"cursor={cursor.id} "
                    f"connection={cursor.connection_id or 'legacy'}"
                )
                continue
            if str(cursor.connector) != str(credential.provider_key):
                cursor_issues.append(
                    f"cursor={cursor.id} connector={cursor.connector} "
                    f"provider={credential.provider_key}"
                )
            if not str(cursor.resource).strip():
                cursor_issues.append(f"cursor={cursor.id} resource=missing")

        issues: list[DataHealthIssue] = []
        if cursor_issues:
            digest = self._stable_issue_hash(":".join(sorted(cursor_issues)))
            issues.append(
                DataHealthIssue(
                    id=f"sync-cursor-integrity:{digest}",
                    category="partial_sync",
                    severity="error",
                    title="Sync-cursors zijn niet meer geldig",
                    description=(
                        "Een of meer sync-cursors verwijzen naar een "
                        "ontbrekende "
                        "connector of hebben een inconsistente resource."
                    ),
                    impact_count=len(cursor_issues),
                    affected_record_count=len(cursor_issues),
                    source="sync_cursor",
                    details=cursor_issues[:100],
                    evidence={
                        "total_count": len(cursor_issues),
                        "detail_limit": 100,
                    },
                    blocking=True,
                    action=action(
                        "view_connection",
                        "/api/v1/connectors/configs",
                        permissions=self._permissions,
                    ),
                )
            )

        run_rows = (
            await self._session.execute(
                select(SyncRun, Credential)
                .join(
                    Credential,
                    and_(
                        Credential.id == SyncRun.connection_id,
                        Credential.tenant_id == self._tenant_id,
                    ),
                )
                .where(
                    SyncRun.status == "completed",
                    SyncRun.report.is_not(None),
                )
                .order_by(SyncRun.completed_at.desc())
                .limit(100)
            )
        ).all()
        failed_count = SyncRun.report["failed"].as_integer()
        skipped_count = SyncRun.report["skipped"].as_integer()
        partial_total_result = await self._session.execute(
            select(func.count(SyncRun.id))
            .join(
                Credential,
                and_(
                    Credential.id == SyncRun.connection_id,
                    Credential.tenant_id == self._tenant_id,
                ),
            )
            .where(
                SyncRun.status == "completed",
                SyncRun.report.is_not(None),
                or_(
                    func.coalesce(failed_count, 0) > 0,
                    func.coalesce(skipped_count, 0) > 0,
                    # PostgreSQL stores the generic SQLAlchemy JSON column as
                    # JSONB in the deployed schema.  Use the JSONB function
                    # explicitly; json_array_length(jsonb) does not exist.
                    func.coalesce(func.jsonb_array_length(SyncRun.warnings), 0)
                    > 0,
                ),
            )
        )
        partial_total = int(partial_total_result.scalar_one() or 0)
        latest_run_cursors: dict[str, datetime] = {}
        latest_cursor_rows = (
            await self._session.execute(
                select(
                    SyncRun.connection_id,
                    func.max(SyncRun.cursor),
                )
                .join(
                    Credential,
                    and_(
                        Credential.id == SyncRun.connection_id,
                        Credential.tenant_id == self._tenant_id,
                    ),
                )
                .where(
                    SyncRun.status == "completed",
                    SyncRun.cursor.is_not(None),
                )
                .group_by(SyncRun.connection_id)
            )
        ).all()
        for connection_id, latest_cursor in latest_cursor_rows:
            if connection_id is not None and latest_cursor is not None:
                latest_run_cursors[str(connection_id)] = latest_cursor

        stale_cursor_issues: list[str] = []
        for cursor, credential in cursor_rows:
            if credential is None or cursor.connection_id is None:
                continue
            latest_run_cursor = latest_run_cursors.get(
                str(cursor.connection_id)
            )
            cursor_value = getattr(cursor, "cursor", None)
            if latest_run_cursor is None or cursor_value is None:
                continue
            if cursor_value < latest_run_cursor:
                stale_cursor_issues.append(
                    f"cursor={cursor.id} connection={cursor.connection_id} "
                    f"resource={cursor.resource} "
                    f"cursor={cursor_value.isoformat()} "
                    f"latest_run={latest_run_cursor.isoformat()}"
                )

        if stale_cursor_issues:
            digest = self._stable_issue_hash(
                ":".join(sorted(stale_cursor_issues))
            )
            issues.append(
                DataHealthIssue(
                    id=f"sync-stale-cursor:{digest}",
                    category="partial_sync",
                    severity="warning",
                    title="Sync-cursor loopt achter op de laatste sync",
                    description=(
                        "Een resource heeft nog een oudere watermark dan de "
                        "laatste succesvolle sync van dezelfde connector. "
                        "Controleer of deze resource in de laatste run is "
                        "overgeslagen voordat je exporteert."
                    ),
                    impact_count=len(stale_cursor_issues),
                    affected_record_count=len(stale_cursor_issues),
                    source="sync_cursor_and_runs",
                    details=stale_cursor_issues[:100],
                    evidence={
                        "total_count": len(stale_cursor_issues),
                        "detail_limit": 100,
                    },
                    blocking=False,
                    action=action(
                        "view_sync_run",
                        "/api/v1/sync-runs",
                        permissions=self._permissions,
                    ),
                )
            )

        partial_runs: list[str] = []
        for run, _credential in run_rows:
            raw_report: object = getattr(run, "report", None)
            report: dict[str, object] = (
                cast("dict[str, object]", raw_report)
                if isinstance(raw_report, dict)
                else {}
            )
            failed_value = report.get("failed")
            skipped_value = report.get("skipped")
            failed = (
                int(failed_value)
                if isinstance(failed_value, (int, float, str))
                else 0
            )
            skipped = (
                int(skipped_value)
                if isinstance(skipped_value, (int, float, str))
                else 0
            )
            warnings = list(run.warnings or [])
            if failed or skipped or warnings:
                partial_runs.append(
                    f"run={run.id} failed={failed} skipped={skipped} "
                    f"warnings={len(warnings)}"
                )
        if partial_runs:
            digest = self._stable_issue_hash(":".join(sorted(partial_runs)))
            issues.append(
                DataHealthIssue(
                    id=f"sync-partial-run:{digest}",
                    category="partial_sync",
                    severity="warning",
                    title=(
                        "Sync is als voltooid gemarkeerd met gedeeltelijke "
                        "output"
                    ),
                    description=(
                        "Een of meer succesvolle sync-runs bevatten "
                        "overgeslagen of mislukte records. Controleer de run "
                        "voordat je exporteert."
                    ),
                    impact_count=partial_total,
                    affected_record_count=partial_total,
                    source="sync_runs",
                    details=partial_runs,
                    evidence={
                        "total_count": partial_total,
                        "detail_limit": 100,
                    },
                    action=action(
                        "view_sync_run",
                        "/api/v1/sync-runs",
                        permissions=self._permissions,
                    ),
                )
            )

        latest_selection_runs: set[str] = set()
        for run, credential in run_rows:
            connection_id = str(getattr(credential, "id", ""))
            if not connection_id or connection_id in latest_selection_runs:
                continue
            latest_selection_runs.add(connection_id)
            selected_values = getattr(credential, "selected_accounts", None)
            raw_report: object = getattr(run, "report", None)
            report: dict[str, object] = (
                cast("dict[str, object]", raw_report)
                if isinstance(raw_report, dict)
                else {}
            )
            returned_values = report.get("account_external_ids")
            if not isinstance(selected_values, list) or not isinstance(
                returned_values, list
            ):
                # Older runs do not have the resource identity extension;
                # absence of that field is not evidence of a missing account.
                continue
            selected_values = cast("list[object]", selected_values)
            returned_values = cast("list[object]", returned_values)
            selected_ids = {
                str(value).strip()
                for value in selected_values
                if str(value).strip()
            }
            returned_ids = {
                str(value).strip()
                for value in returned_values
                if str(value).strip()
            }
            missing_ids = sorted(selected_ids - returned_ids)
            if not missing_ids:
                continue
            digest = self._stable_issue_hash(
                f"{connection_id}:" + "|".join(missing_ids)
            )
            issues.append(
                DataHealthIssue(
                    id=f"sync-selected-account-gap:{digest}",
                    category="partial_sync",
                    severity="warning",
                    title="Geselecteerde accounts ontbraken in de laatste sync",
                    description=(
                        "De laatste succesvolle sync rapporteerde niet alle "
                        "geselecteerde provideraccounts. Controleer of de "
                        "provider deze accounts nog teruggeeft voordat je "
                        "de destination bijwerkt."
                    ),
                    impact_count=len(missing_ids),
                    affected_record_count=len(missing_ids),
                    provider=str(credential.provider_key),
                    connection_id=connection_id,
                    source="sync_runs_and_connection_selection",
                    details=[
                        f"connection={connection_id} missing={external_id}"
                        for external_id in missing_ids[:100]
                    ],
                    evidence={
                        "missing_external_account_ids": missing_ids[:100],
                        "selected_count": len(selected_ids),
                        "returned_count": len(returned_ids),
                        "total_count": len(missing_ids),
                        "detail_limit": 100,
                    },
                    blocking=False,
                    action=action(
                        "view_connection",
                        f"/api/v1/connectors/configs/{connection_id}",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    async def _portfolio_quantity_issues(self) -> list[DataHealthIssue]:
        """Compare trade/transfer-derived quantities with holding snapshots.

        A security-bearing transfer contributes quantity according to its cash
        direction: positive amount is an inbound leg and negative amount is an
        outbound leg.  Transfers without a usable direction are deliberately
        excluded because guessing would create a false integrity failure.

        Split/corporate-action style activities are applied chronologically
        only when their provider metadata contains an explicit positive
        ``split_ratio`` or ``quantity_multiplier`` (new units / old units).
        """
        transaction_rows = (
            await self._session.execute(
                select(
                    Transaction.id,
                    Transaction.account_id,
                    Transaction.security_id,
                    Transaction.transaction_type,
                    Transaction.quantity,
                    Transaction.amount,
                    Transaction.occurred_at,
                    Transaction.provider_metadata_contract,
                )
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.security_id.is_not(None),
                    Transaction.transaction_type.in_(
                        (
                            "purchase",
                            "sale",
                            "transfer",
                            "split",
                            "adjustment",
                            "corporate_action",
                        )
                    ),
                )
                .order_by(
                    Transaction.account_id,
                    Transaction.security_id,
                    Transaction.occurred_at,
                )
            )
        ).all()
        expected: dict[tuple[str, str], Decimal] = {}
        last_activity_at: dict[tuple[str, str], object] = {}
        unmodeled_events: list[tuple[str, str]] = []
        for row in transaction_rows:
            (
                transaction_id,
                account_id,
                security_id,
                transaction_type,
                quantity,
                amount,
                occurred_at,
                metadata,
            ) = row
            key = (str(account_id), str(security_id))
            # The query is ordered chronologically per account/security, so
            # the final value is the latest activity timestamp available for
            # reconciliation evidence.
            last_activity_at[key] = occurred_at
            transaction_type = str(transaction_type)
            if transaction_type in {"split", "adjustment", "corporate_action"}:
                activity_findings = validate_activity_semantics(
                    SimpleNamespace(
                        id=transaction_id,
                        transaction_type=transaction_type,
                        provider_metadata_contract=metadata,
                    )
                )
                ratio = quantity_event_ratio(metadata)
                if (
                    any(
                        finding.category == "invalid_activity_semantics"
                        for finding in activity_findings
                    )
                    or ratio is None
                    or key not in expected
                ):
                    unmodeled_events.append(
                        (str(transaction_id), transaction_type)
                    )
                    continue
                expected[key] = expected.get(key, Decimal(0)) * ratio
                continue

            if quantity is None:
                continue
            quantity_value = _finite_decimal(quantity)
            if quantity_value is None:
                continue
            signed_quantity = quantity_value
            if transaction_type == "sale":
                signed_quantity = -abs(signed_quantity)
            elif transaction_type == "purchase":
                signed_quantity = abs(signed_quantity)
            else:
                amount_value = _finite_decimal(amount)
                if amount_value is None or amount_value == 0:
                    continue
                signed_quantity = (
                    abs(signed_quantity)
                    if amount_value > 0
                    else -abs(signed_quantity)
                )
            expected[key] = expected.get(key, Decimal(0)) + signed_quantity

        latest = (
            select(
                Holding.account_id,
                Holding.security_id,
                func.max(Holding.observed_at).label("latest_observed_at"),
            )
            .where(
                Holding.tenant_id == self._tenant_id,
                Holding.quantity != 0,
            )
            .group_by(Holding.account_id, Holding.security_id)
            .subquery()
        )
        holding_rows = (
            await self._session.execute(
                select(
                    Holding.account_id,
                    Holding.security_id,
                    Holding.quantity,
                    Holding.observed_at,
                )
                .join(
                    latest,
                    and_(
                        latest.c.account_id == Holding.account_id,
                        latest.c.security_id == Holding.security_id,
                        latest.c.latest_observed_at == Holding.observed_at,
                    ),
                )
                .where(Holding.tenant_id == self._tenant_id)
            )
        ).all()
        issues: list[DataHealthIssue] = []
        if unmodeled_events:
            digest = self._stable_issue_hash(
                ":".join(
                    f"{transaction_id}:{event_type}"
                    for transaction_id, event_type in unmodeled_events
                )
            )
            issues.append(
                DataHealthIssue(
                    id=f"portfolio-quantity-evidence:{digest}",
                    category="invalid_activity_semantics",
                    severity="warning",
                    title="Corporate-action quantity is niet bepaalbaar",
                    description=(
                        "Een split/adjustment mist een expliciete ratio of "
                        "een bruikbare quantity-basis. De activiteit is "
                        "daarom niet meegenomen in de verwachte holding "
                        "quantity."
                    ),
                    impact_count=len(unmodeled_events),
                    affected_record_count=len(unmodeled_events),
                    affected_transaction_ids=[
                        transaction_id
                        for transaction_id, _event_type in unmodeled_events[
                            :100
                        ]
                    ],
                    source="transactions",
                    blocking=False,
                    evidence={
                        "insufficient_evidence": True,
                        "total_count": len(unmodeled_events),
                        "detail_limit": 100,
                    },
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions",
                        permissions=self._permissions,
                    ),
                )
            )
        tolerance = Decimal("0.00000001")
        for account_id, security_id, actual, observed_at in holding_rows:
            key = (str(account_id), str(security_id))
            if key not in expected:
                continue
            expected_quantity = expected[key]
            actual_quantity = _finite_decimal(actual)
            if actual_quantity is None:
                continue
            difference = actual_quantity - expected_quantity
            if abs(difference) <= tolerance:
                continue
            digest = self._stable_issue_hash(
                f"{account_id}:{security_id}:{observed_at}"
            )
            percentage_difference = (
                abs(difference) / abs(expected_quantity) * 100
                if expected_quantity != 0
                else None
            )
            percentage_text = None
            if percentage_difference is not None:
                percentage_text = format(percentage_difference, "f")
                if "." in percentage_text:
                    percentage_text = percentage_text.rstrip("0").rstrip(".")
            issues.append(
                DataHealthIssue(
                    id=f"portfolio-quantity:{digest}",
                    category="portfolio_quantity_mismatch",
                    severity="error",
                    title="Holdinghoeveelheid wijkt af van transacties",
                    description=(
                        "De laatste holding-snapshot sluit niet aan op de "
                        "koop-, verkoop- en security-transferactiviteiten in "
                        "de canonical dataset."
                    ),
                    impact_count=1,
                    affected_record_count=1,
                    source="holdings_and_transactions",
                    account_ids=[str(account_id)],
                    security_ids=[str(security_id)],
                    blocking=True,
                    evidence={
                        "expected_quantity": str(expected_quantity),
                        "actual_quantity": str(actual_quantity),
                        "difference": str(difference),
                        "percentage_difference": percentage_text,
                        "last_activity_at": str(last_activity_at.get(key))
                        if key in last_activity_at
                        else None,
                        "last_holding_at": str(observed_at),
                        "observed_at": str(observed_at),
                    },
                    action=action(
                        "view_holdings",
                        "/api/v1/holdings",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    async def _security_identity_issues(self) -> list[DataHealthIssue]:
        """Detect ambiguous or malformed identities used by this tenant."""
        referenced = exists(
            select(Holding.id).where(
                Holding.tenant_id == self._tenant_id,
                Holding.security_id == Security.id,
            )
        ) | exists(
            select(Transaction.id).where(
                Transaction.tenant_id == self._tenant_id,
                Transaction.security_id == Security.id,
            )
        )
        rows = (
            await self._session.execute(
                select(
                    Security.id,
                    Security.isin,
                    Security.ticker,
                    Security.security_type,
                    Security.currency_code,
                )
                .where(referenced)
                .order_by(Security.id)
            )
        ).all()
        listing_rows = (
            await self._session.execute(
                select(
                    SecurityListing.security_id,
                    SecurityListing.mic,
                    SecurityListing.exchange_name,
                    SecurityListing.ticker,
                    SecurityListing.currency_code,
                ).where(
                    SecurityListing.security_id.in_(
                        {str(row[0]) for row in rows}
                    )
                )
            )
        ).all()
        listings_by_security: dict[str, list[tuple[object, ...]]] = {}
        for listing in listing_rows:
            listings_by_security.setdefault(str(listing[0]), []).append(
                tuple(listing)
            )
        issues: list[DataHealthIssue] = []
        ticker_groups: dict[tuple[str, str, str], list[tuple[object, ...]]] = {}
        ticker_variants: dict[str, list[tuple[object, ...]]] = {}
        for row in rows:
            security_id, isin, ticker, security_type, currency = row
            security_id = str(security_id)
            normalized_currency = str(currency or "").upper()
            normalized_ticker = str(ticker or "").strip().upper()
            if normalized_ticker:
                ticker_variants.setdefault(normalized_ticker, []).append(
                    tuple(row)
                )
            if isin and not self._valid_isin(str(isin)):
                identity_digest = self._stable_issue_hash(f"{security_id}:isin")
                issues.append(
                    DataHealthIssue(
                        id=f"security-identity:{identity_digest}",
                        category="security_identity_conflict",
                        severity="error",
                        title="Security heeft een ongeldige ISIN",
                        description=(
                            "De ISIN heeft niet het verwachte formaat en kan "
                            "niet veilig naar een downstream instrument worden "
                            "gemapt."
                        ),
                        impact_count=1,
                        affected_record_count=1,
                        security_ids=[security_id],
                        source="securities",
                        evidence={"identifier_type": "isin"},
                        blocking=True,
                        action=action(
                            "view_holdings",
                            "/api/v1/securities",
                            permissions=self._permissions,
                        ),
                    )
                )
            if (
                not normalized_currency
                or len(normalized_currency) != 3
                or not normalized_currency.isascii()
                or not normalized_currency.isalpha()
            ):
                identity_digest = self._stable_issue_hash(
                    f"{security_id}:currency"
                )
                issues.append(
                    DataHealthIssue(
                        id=f"security-identity:{identity_digest}",
                        category="security_identity_conflict",
                        severity="error",
                        title="Security heeft een ongeldige valuta",
                        description=(
                            "De security-valuta is geen ISO-4217 "
                            "drielettercode."
                        ),
                        impact_count=1,
                        affected_record_count=1,
                        security_ids=[security_id],
                        source="securities",
                        evidence={"identifier_type": "currency_code"},
                        blocking=True,
                        action=action(
                            "view_holdings",
                            "/api/v1/securities",
                            permissions=self._permissions,
                        ),
                    )
                )
            if normalized_ticker and normalized_currency:
                key = (
                    normalized_ticker,
                    normalized_currency,
                    str(security_type or ""),
                )
                ticker_groups.setdefault(key, []).append(tuple(row))

        for key, group in ticker_groups.items():
            identifiers = {str(row[1]).upper() for row in group if row[1]}
            security_ids = sorted({str(row[0]) for row in group})
            if len(security_ids) < 2 or len(identifiers) < 2:
                continue
            digest = self._stable_issue_hash(":".join([*key, *security_ids]))
            issues.append(
                DataHealthIssue(
                    id=f"security-identity:{digest}",
                    category="security_identity_conflict",
                    severity="warning",
                    title="Ticker verwijst naar meerdere security-identiteiten",
                    description=(
                        "Dezelfde ticker/valuta/security-type-combinatie heeft "
                        "meerdere ISIN-identiteiten. Controleer beurs of "
                        "instrumentmapping voordat je exporteert."
                    ),
                    impact_count=len(security_ids),
                    affected_record_count=len(security_ids),
                    security_ids=security_ids,
                    source="securities",
                    evidence={
                        "ticker": key[0],
                        "currency": key[1],
                        "security_type": key[2],
                        "isin_count": len(identifiers),
                    },
                    action=action(
                        "view_holdings",
                        "/api/v1/securities",
                        permissions=self._permissions,
                    ),
                )
            )

        for ticker, group in ticker_variants.items():
            security_ids = sorted({str(row[0]) for row in group})
            variants = {
                (
                    str(row[3] or ""),
                    str(row[4] or "").upper(),
                )
                for row in group
            }
            if len(security_ids) < 2 or len(variants) < 2:
                continue
            digest = self._stable_issue_hash(
                "ambiguous-ticker:" + ticker + ":" + "|".join(security_ids)
            )
            listing_evidence: dict[str, object] = {}
            venue_values = sorted(
                {
                    str(listing[1]).upper()
                    for security_id in security_ids
                    for listing in listings_by_security.get(security_id, [])
                    if listing[1]
                }
            )
            if venue_values:
                listing_evidence = {
                    "listing_venues": venue_values,
                    "listing_count": sum(
                        len(listings_by_security.get(security_id, []))
                        for security_id in security_ids
                    ),
                }
            issues.append(
                DataHealthIssue(
                    id=f"security-identity:{digest}",
                    category="security_identity_conflict",
                    severity="warning",
                    title="Ticker is ambigu over meerdere instrumentvarianten",
                    description=(
                        "Dezelfde ticker wordt gebruikt voor meerdere valuta- "
                        "of security-typevarianten. Omdat exchange/venue niet "
                        "in het canonical model zit, is automatische matching "
                        "niet veilig. Controleer de instrumentmapping vóór "
                        "export."
                    ),
                    impact_count=len(security_ids),
                    affected_record_count=len(security_ids),
                    security_ids=security_ids,
                    source="securities",
                    evidence={
                        "ticker": ticker,
                        "variant_count": len(variants),
                        "currencies": sorted(
                            {currency for _kind, currency in variants}
                        ),
                        "security_types": sorted(
                            {kind for kind, _currency in variants}
                        ),
                        **listing_evidence,
                    },
                    blocking=False,
                    action=action(
                        "view_holdings",
                        "/api/v1/securities",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    async def _destination_parity_issues(self) -> list[DataHealthIssue]:
        """Check the latest local Wealthfolio delivery ledger.

        This is intentionally local and read-only.  Remote destination
        reachability is a separate opt-in check; a Data health GET must not
        block on an external Wealthfolio request.
        """
        mapping_result = await self._session.execute(
            select(ExportTarget).where(
                ExportTarget.tenant_id == self._tenant_id,
                ExportTarget.target_type == "wealthfolio",
            )
        )
        targets = list(mapping_result.scalars())
        issues: list[DataHealthIssue] = []
        for target in targets:
            health_status = str(target.last_health_status or "").lower()
            if health_status not in {
                "degraded",
                "failed",
                "unauthorized",
                "unavailable",
            }:
                continue
            blocking = health_status in {
                "failed",
                "unauthorized",
                "unavailable",
            }
            digest = self._stable_issue_hash(
                f"{target.id}:{health_status}:{target.last_health_error or ''}"
            )
            persisted_summary: dict[str, object] = (
                cast("dict[str, object]", target.last_parity_summary)
                if isinstance(target.last_parity_summary, dict)
                else {}
            )
            raw_counts: object = persisted_summary.get("counts")
            parity_counts = (
                {
                    str(key): max(0, value)
                    for key, value in cast(
                        "dict[object, object]", raw_counts
                    ).items()
                    if key
                    in {
                        "remote_accounts",
                        "unmapped_remote_accounts",
                        "remote_assets",
                        "remote_activities",
                        "canonical_activities",
                        "stale_remote_activities",
                    }
                    and isinstance(key, str)
                    and isinstance(value, int)
                }
                if isinstance(raw_counts, dict)
                else {}
            )
            issues.append(
                DataHealthIssue(
                    id=f"destination-drift:health:{digest}",
                    category="destination_drift",
                    severity="error" if blocking else "warning",
                    title="Wealthfolio destination health vereist aandacht",
                    description=(
                        "De laatste expliciete destination-test rapporteert "
                        "geen volledig gezonde verbinding of parity."
                    ),
                    impact_count=1,
                    affected_record_count=1,
                    source="export_targets",
                    blocking=blocking,
                    evidence={
                        "target_id": str(target.id),
                        "health_status": health_status,
                        "parity_counts": parity_counts,
                    },
                    details=(
                        [str(target.last_health_error)]
                        if target.last_health_error
                        else []
                    ),
                    action=action(
                        "test_destination",
                        f"/api/v1/destinations/{target.id}/test",
                        permissions=self._permissions,
                    ),
                )
            )

        mapping_result = await self._session.execute(
            select(WealthfolioAccountMapping).where(
                WealthfolioAccountMapping.tenant_id == self._tenant_id
            )
        )
        mappings = list(mapping_result.scalars())
        by_target_provider_id: dict[tuple[str, str], list[str]] = {}
        for mapping in mappings:
            provider_id = str(mapping.provider_account_id or "").strip()
            account_id = str(mapping.account_id)
            target_id = str(getattr(mapping, "target_id", None) or "legacy")
            if not provider_id or not str(mapping.wf_account_id or "").strip():
                mapping_digest = self._stable_issue_hash(
                    f"{target_id}:{account_id}"
                )
                issues.append(
                    DataHealthIssue(
                        id=f"destination-drift:mapping:{mapping_digest}",
                        category="destination_drift",
                        severity="warning",
                        title="Wealthfolio-accountmapping is incompleet",
                        description=(
                            "Een canonical account heeft nog geen complete "
                            "Wealthfolio remote mapping."
                        ),
                        impact_count=1,
                        affected_record_count=1,
                        account_ids=[account_id],
                        source="wealthfolio_account_mappings",
                        evidence={
                            "mapping_complete": False,
                            "target_id": target_id,
                        },
                        action=action(
                            "view_export",
                            "/api/v1/destinations",
                            permissions=self._permissions,
                        ),
                    )
                )
                continue
            by_target_provider_id.setdefault(
                (target_id, provider_id), []
            ).append(account_id)
        for (
            target_id,
            provider_id,
        ), account_ids in by_target_provider_id.items():
            if len(set(account_ids)) < 2:
                continue
            digest = self._stable_issue_hash(
                f"{target_id}:{provider_id}:{'|'.join(sorted(account_ids))}"
            )
            issues.append(
                DataHealthIssue(
                    id=f"destination-drift:mapping-conflict:{digest}",
                    category="destination_drift",
                    severity="error",
                    title="Wealthfolio-accountmapping is conflicteerd",
                    description=(
                        "Eén provider-account-id is aan meerdere canonical "
                        "accounts gekoppeld. Dit kan dubbele downstream-data "
                        "veroorzaken."
                    ),
                    impact_count=len(set(account_ids)),
                    affected_record_count=len(set(account_ids)),
                    account_ids=sorted(set(account_ids)),
                    source="wealthfolio_account_mappings",
                    evidence={
                        "mapping_conflict": True,
                        "target_id": target_id,
                    },
                    blocking=True,
                    action=action(
                        "view_export",
                        "/api/v1/destinations",
                        permissions=self._permissions,
                    ),
                )
            )

        result = await self._session.execute(
            select(ExportRun)
            .where(
                ExportRun.tenant_id == self._tenant_id,
                ExportRun.exporter_type == "wealthfolio",
                ExportRun.status == "completed",
            )
            .order_by(ExportRun.completed_at.desc())
            .limit(1)
        )
        runs = list(result.scalars())
        if not runs:
            return issues
        run = runs[0]
        raw_manifest: object = getattr(run, "preflight_manifest", None)
        manifest: dict[str, object] = (
            cast("dict[str, object]", raw_manifest)
            if isinstance(raw_manifest, dict)
            else {}
        )
        raw_post_export = manifest.get("post_export")
        post_export: dict[str, object] = (
            cast("dict[str, object]", raw_post_export)
            if isinstance(raw_post_export, dict)
            else {}
        )
        attempted = int(run.transactions_attempted or 0)
        exported = int(run.transactions_exported or 0)
        failed = int(run.transactions_failed or 0)
        preflight_status = str(manifest.get("status") or "unknown")
        post_status = str(post_export.get("status") or "unknown")
        if (
            failed == 0
            and attempted == exported
            and preflight_status != "blocked"
            and post_status not in {"degraded", "failed"}
        ):
            return issues

        blocking = preflight_status == "blocked" or failed > 0
        digest = self._stable_issue_hash(
            f"{run.id}:{attempted}:{exported}:{failed}:"
            f"{preflight_status}:{post_status}"
        )
        issues.append(
            DataHealthIssue(
                id=f"destination-drift:{digest}",
                category="destination_drift",
                severity="error" if blocking else "warning",
                title="Laatste Wealthfolio-delivery wijkt af",
                description=(
                    "De laatste lokale Wealthfolio-export heeft niet alle "
                    "activiteiten succesvol afgeleverd of is gedegradeerd."
                ),
                impact_count=max(failed, attempted - exported, 1),
                affected_record_count=max(failed, attempted - exported, 1),
                source="export_runs",
                blocking=blocking,
                evidence={
                    "run_status": str(run.status),
                    "transactions_attempted": attempted,
                    "transactions_exported": exported,
                    "transactions_failed": failed,
                    "preflight_status": preflight_status,
                    "post_export_status": post_status,
                },
                action=action(
                    "view_export",
                    f"/api/v1/exporters/runs/{run.id}",
                    permissions=self._permissions,
                ),
            )
        )
        return issues

    @staticmethod
    def _valid_isin(value: str) -> bool:
        """Validate the structural ISIN shape without exposing identifiers."""
        normalized = value.strip().upper()
        return (
            len(normalized) == 12
            and normalized[:2].isalpha()
            and normalized[-1].isdigit()
            and normalized.isalnum()
        )

    async def _tax_lot_integrity_issues(self) -> list[DataHealthIssue]:
        """Find impossible or internally contradictory tax-lot quantities."""
        rows: list[TaxLot] = list(
            (
                await self._session.execute(
                    select(TaxLot).where(TaxLot.tenant_id == self._tenant_id)
                )
            ).scalars()
        )
        transaction_rows = (
            await self._session.execute(
                select(
                    Transaction.id,
                    Transaction.account_id,
                    Transaction.security_id,
                    Transaction.transaction_type,
                    Transaction.quantity,
                ).where(Transaction.tenant_id == self._tenant_id)
            )
        ).all()
        transactions = {
            str(row[0]): (
                str(row[1]),
                str(row[2]) if row[2] else None,
                str(row[3]),
            )
            for row in transaction_rows
        }
        sales_by_key: dict[tuple[str, str], Decimal] = {}
        invalid_sales: list[tuple[str, str, str]] = []
        for row in transaction_rows:
            if str(row[3]) != "sale" or row[4] is None or row[2] is None:
                continue
            sale_quantity = _finite_decimal(row[4])
            if sale_quantity is None:
                invalid_sales.append((str(row[0]), str(row[1]), str(row[2])))
                continue
            key = (str(row[1]), str(row[2]))
            sales_by_key[key] = sales_by_key.get(key, Decimal(0)) + abs(
                sale_quantity
            )
        account_ids = {
            str(row[0])
            for row in (
                await self._session.execute(
                    select(Account.id).where(
                        Account.tenant_id == self._tenant_id
                    )
                )
            ).all()
        }
        security_ids = {
            str(row[0])
            for row in (
                await self._session.execute(
                    select(Security.id).where(
                        Security.id.is_not(None),
                    )
                )
            ).all()
        }
        broken: list[TaxLot] = []
        basis_missing: list[TaxLot] = []
        transfer_origin_invalid: dict[str, str] = {}
        cost_basis_invalid: dict[str, str] = {}
        quantity_invalid: dict[str, str] = {}
        lot_capacity: dict[tuple[str, str], Decimal] = {}
        for lot in rows:
            quantity_value = _finite_decimal(lot.quantity)
            remaining_value = _finite_decimal(lot.remaining_quantity)
            quantity = quantity_value or Decimal(0)
            remaining = remaining_value or Decimal(0)
            if quantity_value is None:
                quantity_invalid[str(lot.id)] = "invalid_quantity"
            elif remaining_value is None:
                quantity_invalid[str(lot.id)] = "invalid_remaining_quantity"
            elif quantity_value <= 0:
                quantity_invalid[str(lot.id)] = "non_positive_quantity"
            lot_key = (str(lot.account_id), str(lot.security_id))
            if quantity_value is not None:
                lot_capacity[lot_key] = lot_capacity.get(
                    lot_key, Decimal(0)
                ) + max(quantity, Decimal(0))
            relationship_error = (
                str(lot.account_id) not in account_ids
                or str(lot.security_id) not in security_ids
            )
            for transaction_id, expected_type in (
                (
                    getattr(lot, "purchase_transaction_id", None),
                    "purchase",
                ),
                (getattr(lot, "sale_transaction_id", None), "sale"),
            ):
                if transaction_id is None:
                    continue
                transaction = transactions.get(str(transaction_id))
                if transaction is None or transaction != (
                    str(lot.account_id),
                    str(lot.security_id),
                    expected_type,
                ):
                    relationship_error = True
            transfer_transaction_id = getattr(
                lot, "transfer_transaction_id", None
            )
            if transfer_transaction_id is not None:
                transfer_transaction = transactions.get(
                    str(transfer_transaction_id)
                )
                if transfer_transaction is None:
                    transfer_origin_invalid[str(lot.id)] = (
                        "missing_transfer_transaction"
                    )
                    relationship_error = True
                elif (
                    transfer_transaction[1] != str(lot.security_id)
                    or transfer_transaction[2] != "transfer"
                ):
                    transfer_origin_invalid[str(lot.id)] = (
                        "invalid_transfer_transaction"
                    )
                    relationship_error = True
            cost_basis_reason: str | None = None
            cost_basis_total = getattr(lot, "cost_basis_total", None)
            cost_basis_per_unit = getattr(lot, "cost_basis_per_unit", None)
            cost_basis_total_decimal: Decimal | None = None
            cost_basis_per_unit_decimal: Decimal | None = None
            if cost_basis_total is not None:
                try:
                    cost_basis_total_decimal = Decimal(str(cost_basis_total))
                    if cost_basis_total_decimal < 0:
                        cost_basis_reason = "negative_cost_basis_total"
                except (InvalidOperation, ValueError):
                    cost_basis_reason = "invalid_cost_basis_total"
            if cost_basis_reason is None and cost_basis_per_unit is not None:
                try:
                    cost_basis_per_unit_decimal = Decimal(
                        str(cost_basis_per_unit)
                    )
                    if cost_basis_per_unit_decimal < 0:
                        cost_basis_reason = "negative_cost_basis_per_unit"
                except (InvalidOperation, ValueError):
                    cost_basis_reason = "invalid_cost_basis_per_unit"
            if (
                cost_basis_reason is None
                and cost_basis_total_decimal is not None
                and cost_basis_per_unit_decimal is not None
                and quantity > 0
                and abs(
                    cost_basis_total_decimal
                    - (cost_basis_per_unit_decimal * quantity)
                )
                > Decimal("0.01")
            ):
                cost_basis_reason = "cost_basis_unit_mismatch"
            lot_currency = str(getattr(lot, "currency_code", "") or "").upper()
            # Legacy test doubles may omit the model field; a real TaxLot
            # always has it, so only validate a supplied value here.
            if (
                cost_basis_reason is None
                and hasattr(lot, "currency_code")
                and (
                    not lot_currency
                    or len(lot_currency) != 3
                    or not lot_currency.isascii()
                    or not lot_currency.isalpha()
                )
            ):
                cost_basis_reason = "invalid_lot_currency"
            if cost_basis_reason is not None:
                cost_basis_invalid[str(lot.id)] = cost_basis_reason
            structural_error = (
                quantity_value is None
                or remaining_value is None
                or quantity <= 0
                or remaining < 0
                or remaining > quantity
                or (quantity > 0 and remaining == 0 and lot.closed_at is None)
                or (lot.closed_at is not None and remaining > 0)
                or relationship_error
                or cost_basis_reason is not None
            )
            if structural_error:
                broken.append(lot)
            elif (
                getattr(lot, "purchase_transaction_id", None) is None
                and transfer_transaction_id is None
            ):
                basis_missing.append(lot)
        oversold = [
            (key, sales, lot_capacity[key])
            for key, sales in sales_by_key.items()
            if key in lot_capacity and sales > lot_capacity[key]
        ]
        unbacked_sales = [
            (key, sales)
            for key, sales in sales_by_key.items()
            if key not in lot_capacity
        ]
        if (
            not broken
            and not oversold
            and not basis_missing
            and not unbacked_sales
            and not invalid_sales
        ):
            return []
        digest = self._stable_issue_hash(
            ":".join(
                [
                    *(str(lot.id) for lot in broken),
                    *(
                        f"oversold:{account_id}:{security_id}"
                        for (
                            (account_id, security_id),
                            _sales,
                            _capacity,
                        ) in oversold
                    ),
                    *(f"basis-missing:{lot.id}" for lot in basis_missing),
                    *(
                        f"cost-basis:{lot_id}:{reason}"
                        for lot_id, reason in sorted(cost_basis_invalid.items())
                    ),
                    *(
                        f"quantity:{lot_id}:{reason}"
                        for lot_id, reason in sorted(quantity_invalid.items())
                    ),
                    *(
                        f"transfer-origin:{lot_id}:{reason}"
                        for lot_id, reason in sorted(
                            transfer_origin_invalid.items()
                        )
                    ),
                    *(
                        f"invalid-sale:{transaction_id}"
                        for (
                            transaction_id,
                            _account_id,
                            _security_id,
                        ) in invalid_sales
                    ),
                    *(
                        f"unbacked-sale:{account_id}:{security_id}"
                        for (account_id, security_id), _sales in unbacked_sales
                    ),
                ]
            )
        )
        all_flagged_lots = [*broken, *basis_missing]
        affected_accounts = (
            {str(lot.account_id) for lot in all_flagged_lots}
            | {
                account_id
                for (account_id, _security_id), _sales, _capacity in oversold
            }
            | {
                account_id
                for (account_id, _security_id), _sales in unbacked_sales
            }
            | {account_id for _id, account_id, _security_id in invalid_sales}
        )
        affected_securities = (
            {str(lot.security_id) for lot in all_flagged_lots}
            | {
                security_id
                for (_account_id, security_id), _sales, _capacity in oversold
            }
            | {
                security_id
                for (_account_id, security_id), _sales in unbacked_sales
            }
            | {security_id for _id, _account_id, security_id in invalid_sales}
        )
        evidence: dict[str, object] = {
            "total_count": len(broken)
            + len(oversold)
            + len(basis_missing)
            + len(unbacked_sales)
            + len(invalid_sales),
            "detail_limit": 100,
            "broken_lot_count": len(broken),
            "oversold_group_count": len(oversold),
            "missing_basis_count": len(basis_missing),
            **(
                {"invalid_transfer_origin_count": len(transfer_origin_invalid)}
                if transfer_origin_invalid
                else {}
            ),
            **(
                {"cost_basis_error_count": len(cost_basis_invalid)}
                if cost_basis_invalid
                else {}
            ),
            **(
                {"quantity_error_count": len(quantity_invalid)}
                if quantity_invalid
                else {}
            ),
            **(
                {"invalid_sale_quantity_count": len(invalid_sales)}
                if invalid_sales
                else {}
            ),
            **(
                {
                    "unbacked_sale_group_count": len(unbacked_sales),
                    "insufficient_evidence": True,
                }
                if unbacked_sales
                else {}
            ),
        }
        return [
            DataHealthIssue(
                id=f"tax-lot-integrity:{digest}",
                category="tax_lot_integrity",
                title="Tax-lotbasis is onvolledig of inconsistent",
                description=(
                    "Een of meer tax lots hebben een negatieve hoeveelheid, "
                    "meer resterende units dan oorspronkelijk, zijn gesloten "
                    "met een positieve resterende hoeveelheid, of verkopen "
                    "overschrijden de beschikbare lotbasis. Verkopen zonder "
                    "lotbasis worden als onvoldoende bewijs gemeld."
                ),
                impact_count=(
                    len(broken)
                    + len(oversold)
                    + len(basis_missing)
                    + len(unbacked_sales)
                    + len(invalid_sales)
                ),
                affected_record_count=(
                    len(broken)
                    + len(oversold)
                    + len(basis_missing)
                    + len(unbacked_sales)
                    + len(invalid_sales)
                ),
                source="tax_lots",
                account_ids=sorted(affected_accounts),
                security_ids=sorted(affected_securities),
                details=[
                    (
                        f"lot={lot.id} quantity={lot.quantity} "
                        f"remaining={lot.remaining_quantity} "
                        f"closed={'yes' if lot.closed_at else 'no'}"
                    )
                    for lot in broken[:100]
                ]
                + [
                    (
                        f"account={account_id} security={security_id} "
                        f"sales={sales} lot_capacity={capacity}"
                    )
                    for (account_id, security_id), sales, capacity in oversold[
                        :100
                    ]
                ]
                + [
                    f"lot={lot.id} purchase_transaction=missing"
                    for lot in basis_missing[:100]
                ]
                + [
                    f"lot={lot_id} transfer_origin_error={reason}"
                    for lot_id, reason in sorted(
                        transfer_origin_invalid.items()
                    )
                ][:100]
                + [
                    f"lot={lot_id} cost_basis_error={reason}"
                    for lot_id, reason in sorted(cost_basis_invalid.items())
                ][:100]
                + [
                    f"lot={lot_id} quantity_error={reason}"
                    for lot_id, reason in sorted(quantity_invalid.items())
                ][:100]
                + [
                    (
                        f"transaction={transaction_id} account={account_id} "
                        f"security={security_id} sale_quantity=invalid"
                    )
                    for transaction_id, account_id, security_id in (
                        invalid_sales[:100]
                    )
                ]
                + [
                    (
                        f"account={account_id} security={security_id} "
                        f"sales={sales} lot_capacity=missing"
                    )
                    for (account_id, security_id), sales in unbacked_sales[:100]
                ],
                severity=(
                    "error"
                    if broken or oversold or invalid_sales
                    else "warning"
                ),
                blocking=bool(broken or oversold or invalid_sales),
                evidence=evidence,
                action=action(
                    "view_holdings",
                    "/api/v1/holdings",
                    permissions=self._permissions,
                ),
            )
        ]

    async def _cash_reconciliation_issues(self) -> list[DataHealthIssue]:
        """Compare account cash with the latest provider balance snapshot."""
        account_rows = list(
            (
                await self._session.execute(
                    select(Account).where(
                        Account.tenant_id == self._tenant_id,
                        Account.is_active.is_(True),
                        Account.current_balance.is_not(None),
                    )
                )
            ).scalars()
        )
        balance_rows = list(
            (
                await self._session.execute(
                    select(Balance)
                    .where(
                        Balance.tenant_id == self._tenant_id,
                        Balance.balance_kind.in_(("current", "cash", "booked")),
                    )
                    .order_by(Balance.observed_at.desc())
                )
            ).scalars()
        )
        priority = {"current": 0, "cash": 1, "booked": 2}
        latest: dict[tuple[str, str], Balance] = {}
        for balance in balance_rows:
            key = (str(balance.account_id), str(balance.currency_code).upper())
            current = latest.get(key)
            if current is None or (
                balance.observed_at,
                -priority.get(str(balance.balance_kind), 99),
            ) > (
                current.observed_at,
                -priority.get(str(current.balance_kind), 99),
            ):
                latest[key] = balance

        transaction_rows = (
            await self._session.execute(
                select(
                    Transaction.account_id,
                    Transaction.amount,
                    Transaction.transaction_type,
                    Transaction.currency_code,
                    Transaction.occurred_at,
                ).where(Transaction.tenant_id == self._tenant_id)
            )
        ).all()
        cash_types = {
            "deposit",
            "withdrawal",
            "dividend",
            "interest",
            "fee",
            "tax",
            "transfer",
        }
        issues: list[DataHealthIssue] = []
        tolerance = Decimal("0.01")
        for account in account_rows:
            currency = str(account.currency_code).upper()
            balance = latest.get((str(account.id), currency))
            if balance is None:
                issues.append(
                    DataHealthIssue(
                        id=f"cash-reconciliation-insufficient:{account.id}:{currency}",
                        category="cash_reconciliation_mismatch",
                        severity="warning",
                        title="Cashreconciliatie heeft onvoldoende bewijs",
                        description=(
                            "Er is geen actuele provider balance-snapshot in "
                            "de accountvaluta. De cashafwijking kan daarom "
                            "niet betrouwbaar worden berekend."
                        ),
                        impact_count=1,
                        affected_record_count=1,
                        source="accounts_and_balances",
                        account_ids=[str(account.id)],
                        blocking=False,
                        evidence={
                            "insufficient_evidence": True,
                            "reason": "missing_balance_snapshot",
                            "currency": currency,
                            "account_balance": str(account.current_balance),
                            "total_count": 1,
                            "detail_limit": 0,
                        },
                        action=action(
                            "view_accounts",
                            "/api/v1/accounts",
                            permissions=self._permissions,
                        ),
                    )
                )
                continue
            account_value = Decimal(str(account.current_balance))
            snapshot_value = Decimal(str(balance.amount))
            difference = snapshot_value - account_value
            if abs(difference) > tolerance:
                digest = self._stable_issue_hash(
                    f"{account.id}:{balance.id}:{balance.observed_at}"
                )
                issues.append(
                    DataHealthIssue(
                        id=f"cash-reconciliation:{digest}",
                        category="cash_reconciliation_mismatch",
                        severity="error",
                        title="Accountsaldo wijkt af van balance-snapshot",
                        description=(
                            "Het actuele accountsaldo verschilt van de "
                            "laatste provider balance-snapshot in dezelfde "
                            "valuta."
                        ),
                        impact_count=1,
                        affected_record_count=1,
                        source="accounts_and_balances",
                        account_ids=[str(account.id)],
                        blocking=True,
                        evidence={
                            "account_balance": str(account_value),
                            "snapshot_balance": str(snapshot_value),
                            "difference": str(difference),
                            "currency": currency,
                            "observed_at": str(balance.observed_at),
                            "total_count": 1,
                            "detail_limit": 0,
                        },
                        action=action(
                            "view_accounts",
                            "/api/v1/accounts",
                            permissions=self._permissions,
                        ),
                    )
                )

            account_balances = sorted(
                (
                    item
                    for item in balance_rows
                    if str(item.account_id) == str(account.id)
                    and str(item.currency_code).upper() == currency
                ),
                key=lambda item: item.observed_at,
            )
            opening = account_balances[0] if account_balances else None
            if opening is None or opening.id == balance.id:
                continue
            expected = Decimal(str(opening.amount))
            flow_count = 0
            for (
                transaction_account_id,
                amount,
                transaction_type,
                transaction_currency,
                occurred_at,
            ) in transaction_rows:
                if (
                    str(transaction_account_id) != str(account.id)
                    or str(transaction_currency).upper() != currency
                    or str(transaction_type) not in cash_types
                    or occurred_at <= opening.observed_at
                    or occurred_at > balance.observed_at
                    or amount is None
                ):
                    continue
                amount_value = _finite_decimal(amount)
                if amount_value is None:
                    continue
                if str(transaction_type) in {"withdrawal", "fee", "tax"}:
                    amount_value = -abs(amount_value)
                expected += amount_value
                flow_count += 1
            flow_difference = snapshot_value - expected
            if abs(flow_difference) <= tolerance:
                continue
            flow_digest = self._stable_issue_hash(
                f"{account.id}:{opening.id}:{balance.id}:{flow_difference}"
            )
            issues.append(
                DataHealthIssue(
                    id=f"cash-reconciliation-flow:{flow_digest}",
                    category="cash_reconciliation_mismatch",
                    severity="error",
                    title="Cashstroom wijkt af van balance-snapshots",
                    description=(
                        "De openingssnapshot plus canonical cashactiviteiten "
                        "sluit niet aan op de volgende provider snapshot."
                    ),
                    impact_count=1,
                    affected_record_count=1,
                    source="balances_and_transactions",
                    account_ids=[str(account.id)],
                    blocking=True,
                    evidence={
                        "opening_balance": str(opening.amount),
                        "expected_balance": str(expected),
                        "snapshot_balance": str(snapshot_value),
                        "difference": str(flow_difference),
                        "currency": currency,
                        "opening_observed_at": str(opening.observed_at),
                        "observed_at": str(balance.observed_at),
                        "transaction_count": flow_count,
                        "total_count": 1,
                        "detail_limit": 0,
                    },
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    @staticmethod
    def _stable_issue_hash(value: str) -> str:
        return sha256(value.encode("utf-8")).hexdigest()[:16]

    def _account_identity_issue(
        self,
        provider: str,
        identity: str,
        accounts: list[Account],
        *,
        blocking: bool,
    ) -> DataHealthIssue:
        connection_ids = sorted(
            {str(account.connection_id or "legacy") for account in accounts}
        )
        account_ids = [str(account.id) for account in accounts]
        digest = self._stable_issue_hash(
            f"{provider}:{'|'.join(connection_ids)}:{identity}"
        )
        return DataHealthIssue(
            id=f"account-identity:{provider}:{digest}",
            category="account_identity_conflict",
            severity="error" if blocking else "warning",
            title="Mogelijke dubbele provideraccount",
            description=(
                f"{len(accounts)} actieve lokale accounts van {provider} "
                "kunnen naar dezelfde provideraccount verwijzen."
            ),
            impact_count=len(accounts),
            affected_record_count=len(accounts),
            provider=provider,
            source="accounts",
            account_ids=account_ids,
            evidence={
                "external_account_ids": sorted(
                    {str(account.external_account_id) for account in accounts}
                ),
                "connection_ids": connection_ids,
                "currencies": sorted(
                    {str(account.currency_code) for account in accounts}
                ),
            },
            blocking=blocking,
            action=action(
                "view_accounts",
                "/api/v1/accounts",
                permissions=self._permissions,
            ),
        )

    def _missing_source_issues(
        self,
        sources: list[DataHealthSource],
        control: object,
    ) -> list[DataHealthIssue]:
        """Create a sync action when a healthy configured source has no data."""
        connections = getattr(control, "connections", [])
        issues: list[DataHealthIssue] = []
        if not sources:
            return [
                DataHealthIssue(
                    id="missing-sources:configured",
                    category="missing_transactions",
                    severity="warning",
                    title="Nog geen financiële bron geconfigureerd",
                    description=(
                        "Voeg ten minste één bron toe om transacties en saldi "
                        "te kunnen synchroniseren."
                    ),
                    source="connections",
                    action=action(
                        "view_connection",
                        "/api/v1/connectors/configs",
                        permissions=self._permissions,
                    ),
                )
            ]
        for source, connection in zip(sources, connections, strict=True):
            if source.transactions:
                continue
            is_file_source = connection.provider in {
                "degiro_pension",
                "saxo_investor",
            }
            if source.status != "healthy" and not is_file_source:
                continue
            if is_file_source:
                source_action = action(
                    "open_file_import",
                    f"#uploads?provider={connection.provider}",
                    permissions=self._permissions,
                )
                description = (
                    f"De bestandsbron {source.provider} heeft nog geen "
                    "transacties. Upload de actuele provider-export om de "
                    "dataset aan te vullen."
                )
            else:
                source_action = next(
                    (
                        item
                        for item in connection.actions
                        if item.key == "sync_connection"
                    ),
                    action(
                        "sync_connection",
                        f"/api/v1/sync/connections/{source.id}",
                        permissions=self._permissions,
                    ),
                )
                description = (
                    f"De gezonde bron {source.provider} heeft nog geen "
                    "transacties in de canonieke dataset."
                )
            issues.append(
                DataHealthIssue(
                    id=f"missing-transactions:{source.id}",
                    category="missing_transactions",
                    severity="warning",
                    title="Bron bevat nog geen transacties",
                    description=description,
                    provider=source.provider,
                    source="transactions",
                    action=source_action,
                )
            )
        return issues

    async def _wealthfolio_preflight_issues(self) -> list[DataHealthIssue]:
        """Find defects that Wealthfolio cannot represent safely.

        Wealthfolio derives performance from activities, valuations and
        market-data records.  These checks run against the canonical source
        data before export, so the GUI can explain and repair the input rather
        than exposing a misleading destination-side warning.
        """
        issues: list[DataHealthIssue] = []

        quote_rows = (
            await self._session.execute(
                select(Security.name, EnrichmentFreshness.error_message)
                .join(
                    EnrichmentFreshness,
                    EnrichmentFreshness.security_id == Security.id,
                )
                .join(Holding, Holding.security_id == Security.id)
                .where(
                    Holding.tenant_id == self._tenant_id,
                    EnrichmentFreshness.status == "failed",
                    EnrichmentFreshness.data_source == "wealthfolio",
                )
                .distinct()
                .order_by(Security.name)
            )
        ).all()
        if quote_rows:
            issues.append(
                DataHealthIssue(
                    id="wealthfolio:quote-sync-failures",
                    category="quote_sync_failure",
                    severity="error",
                    title="Koerssync naar Wealthfolio mislukt",
                    description=(
                        "Een of meer actuele of historische koersen konden "
                        "niet worden opgeslagen. Controleer ISIN, beurs, "
                        "valuta en de ingestelde marktdata-provider voordat "
                        "je exporteert."
                    ),
                    impact_count=len(quote_rows),
                    affected_record_count=len(quote_rows),
                    source="market_data",
                    details=[
                        f"{name}: {error or 'onbekende fout'}"
                        for name, error in quote_rows[:10]
                    ],
                    evidence={
                        "total_count": len(quote_rows),
                        "detail_limit": 10,
                    },
                    action=action(
                        "refresh_quotes",
                        "/api/v1/enrichment/refresh-quotes",
                        permissions=self._permissions,
                    ),
                )
            )

        negative_rows = (
            await self._session.execute(
                select(
                    Account.name,
                    func.date(Holding.observed_at),
                    func.sum(Holding.market_value),
                )
                .join(Account, Account.id == Holding.account_id)
                .where(
                    Holding.tenant_id == self._tenant_id,
                    Holding.market_value.is_not(None),
                )
                .group_by(Account.name, func.date(Holding.observed_at))
                .having(func.sum(Holding.market_value) < 0)
                .order_by(func.date(Holding.observed_at))
            )
        ).all()
        cash_rows = (
            await self._session.execute(
                select(
                    Transaction.id,
                    Transaction.account_id,
                    Account.name,
                    Transaction.amount,
                    Transaction.occurred_at,
                    Transaction.transaction_type,
                )
                .join(Account, Account.id == Transaction.account_id)
                .where(Transaction.tenant_id == self._tenant_id)
                .order_by(Transaction.account_id, Transaction.occurred_at)
            )
        ).all()
        running_cash: dict[str, Decimal] = {}
        negative_cash: list[tuple[str, str, object, Decimal]] = []
        for (
            _transaction_id,
            account_id,
            name,
            amount,
            occurred_at,
            _transaction_type,
        ) in cash_rows:
            balance = running_cash.get(str(account_id), Decimal(0))
            amount_value = _finite_decimal(amount)
            if amount_value is None:
                continue
            balance += amount_value
            running_cash[str(account_id)] = balance
            if balance < 0:
                negative_cash.append(
                    (str(account_id), str(name), occurred_at, balance)
                )
        negative_history = [*negative_rows, *negative_cash]
        if negative_history:
            issues.append(
                DataHealthIssue(
                    id="wealthfolio:negative-valuation-history",
                    category="negative_valuation",
                    severity="error",
                    title="Negatieve portfoliowaardering in historie",
                    description=(
                        "Wealthfolio kan rendement niet betrouwbaar berekenen "
                        "als een historische waardering onder nul komt. Dit "
                        "wijst meestal op ontbrekende aankoop-, stortings- of "
                        "transferactiviteiten."
                    ),
                    impact_count=len(negative_history),
                    affected_record_count=len(negative_history),
                    source="holdings_and_transactions",
                    details=[
                        (
                            f"{row[0]} · {row[1]}: {row[2]}"
                            if len(row) == 3
                            else f"{row[1]} · {row[2]}: {row[3]}"
                        )
                        for row in negative_history[:10]
                    ],
                    evidence={
                        "total_count": len(negative_history),
                        "detail_limit": 10,
                    },
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions",
                        permissions=self._permissions,
                    ),
                )
            )

        valuation_rows = (
            await self._session.execute(
                select(Holding.id, Account.name, Security.name)
                .add_columns(func.count(Holding.id).over().label("total_count"))
                .join(Account, Account.id == Holding.account_id)
                .join(Security, Security.id == Holding.security_id)
                .where(
                    Holding.tenant_id == self._tenant_id,
                    Holding.quantity != 0,
                    Holding.market_value.is_(None),
                    Holding.price.is_(None),
                )
                .order_by(Holding.observed_at.desc())
                .limit(1000)
            )
        ).all()
        valuation_total = (
            int(valuation_rows[0][3])
            if valuation_rows and len(tuple(valuation_rows[0])) > 3
            else len(valuation_rows)
        )
        if valuation_rows:
            issues.append(
                DataHealthIssue(
                    id="wealthfolio:incomplete-valuations",
                    category="incomplete_valuation",
                    severity="error",
                    title="Waarderingsregels zijn niet compleet",
                    description=(
                        "Elke snapshot die naar Wealthfolio gaat moet een "
                        "marktwaarde of een betrouwbare koers hebben. Vul de "
                        "koers aan of accepteer de security expliciet als "
                        "marktdata niet beschikbaar is."
                    ),
                    impact_count=valuation_total,
                    affected_record_count=valuation_total,
                    source="holdings",
                    details=[
                        f"{row[2]} in {row[1]}" for row in valuation_rows[:10]
                    ],
                    evidence={
                        "total_count": valuation_total,
                        "detail_limit": 1000,
                    },
                    action=action(
                        "refresh_quotes",
                        "/api/v1/enrichment/refresh-quotes",
                        permissions=self._permissions,
                    ),
                )
            )

        latest_holdings = (
            select(
                Holding.account_id,
                Holding.security_id,
                func.max(Holding.observed_at).label("latest_observed_at"),
            )
            .where(
                Holding.tenant_id == self._tenant_id,
                Holding.quantity != 0,
            )
            .group_by(Holding.account_id, Holding.security_id)
            .subquery()
        )
        cost_rows = (
            await self._session.execute(
                select(Holding.id, Account.name, Security.name)
                .add_columns(func.count(Holding.id).over().label("total_count"))
                .join(Account, Account.id == Holding.account_id)
                .join(Security, Security.id == Holding.security_id)
                .join(
                    latest_holdings,
                    and_(
                        latest_holdings.c.account_id == Holding.account_id,
                        latest_holdings.c.security_id == Holding.security_id,
                        latest_holdings.c.latest_observed_at
                        == Holding.observed_at,
                    ),
                )
                .where(
                    Holding.tenant_id == self._tenant_id,
                    Holding.cost_basis.is_(None),
                    ~exists(
                        select(TaxLot.id).where(
                            TaxLot.tenant_id == self._tenant_id,
                            TaxLot.account_id == Holding.account_id,
                            TaxLot.security_id == Holding.security_id,
                            TaxLot.remaining_quantity > 0,
                        )
                    ),
                )
                .order_by(Security.name)
                .limit(1000)
            )
        ).all()
        cost_total = (
            int(cost_rows[0][3])
            if cost_rows and len(tuple(cost_rows[0])) > 3
            else len(cost_rows)
        )
        if cost_rows:
            issues.append(
                DataHealthIssue(
                    id="wealthfolio:incomplete-cost-basis",
                    category="incomplete_cost_basis",
                    severity="warning",
                    title="Posities missen cost basis",
                    description=(
                        "Wealthfolio kan de marktwaarde tonen, maar geen "
                        "betrouwbare winst/verliesberekening maken zonder "
                        "verkrijgingsprijs. Herstel de aankoopactiviteiten of "
                        "leg de verkrijgingsprijs vast."
                    ),
                    impact_count=cost_total,
                    affected_record_count=cost_total,
                    source="holdings",
                    details=[f"{row[2]} in {row[1]}" for row in cost_rows[:10]],
                    evidence={
                        "total_count": cost_total,
                        "detail_limit": 1000,
                    },
                    affected_transaction_ids=[],
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions?type=purchase",
                        permissions=self._permissions,
                    ),
                )
            )
        unverified_cost_rows = (
            await self._session.execute(
                select(Holding.id, Account.name, Security.name)
                .add_columns(func.count(Holding.id).over().label("total_count"))
                .join(Account, Account.id == Holding.account_id)
                .join(Security, Security.id == Holding.security_id)
                .join(
                    latest_holdings,
                    and_(
                        latest_holdings.c.account_id == Holding.account_id,
                        latest_holdings.c.security_id == Holding.security_id,
                        latest_holdings.c.latest_observed_at
                        == Holding.observed_at,
                    ),
                )
                .where(
                    Holding.tenant_id == self._tenant_id,
                    Holding.cost_basis.is_not(None),
                    ~exists(
                        select(TaxLot.id).where(
                            TaxLot.tenant_id == self._tenant_id,
                            TaxLot.account_id == Holding.account_id,
                            TaxLot.security_id == Holding.security_id,
                            TaxLot.remaining_quantity > 0,
                        )
                    ),
                    ~exists(
                        select(Transaction.id).where(
                            Transaction.tenant_id == self._tenant_id,
                            Transaction.account_id == Holding.account_id,
                            Transaction.security_id == Holding.security_id,
                            Transaction.transaction_type == "purchase",
                        )
                    ),
                )
                .order_by(Security.name)
                .limit(1000)
            )
        ).all()
        unverified_cost_total = (
            int(unverified_cost_rows[0][3])
            if unverified_cost_rows and len(tuple(unverified_cost_rows[0])) > 3
            else len(unverified_cost_rows)
        )
        if unverified_cost_rows:
            issues.append(
                DataHealthIssue(
                    id="wealthfolio:unverified-cost-basis",
                    category="incomplete_cost_basis",
                    severity="warning",
                    title="Posities hebben niet-geverifieerde cost basis",
                    description=(
                        "De holding bevat een cost basis, maar er is geen "
                        "open tax lot of aankooptransactie waarmee deze "
                        "basis in de canonical dataset kan worden herleid."
                    ),
                    impact_count=unverified_cost_total,
                    affected_record_count=unverified_cost_total,
                    source="holdings_and_transactions",
                    details=[
                        f"{row[2]} in {row[1]}"
                        for row in unverified_cost_rows[:10]
                    ],
                    evidence={
                        "total_count": unverified_cost_total,
                        "detail_limit": 1000,
                        "basis_present": True,
                        "basis_source": "unverified",
                    },
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions?type=purchase",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    async def _additional_issues(self) -> list[DataHealthIssue]:
        """Detect account conflicts, incomplete imports and exports."""
        issues: list[DataHealthIssue] = []

        # ExportRun is the authoritative run ledger for both legacy
        # environment-based exports and wizard-created destinations.  The
        # control-plane destination projection only knows wizard targets, so
        # querying the ledger here also exposes legacy Wealthfolio failures on
        # the Data health page.
        export_rows = (
            (
                await self._session.execute(
                    select(ExportRun)
                    .where(ExportRun.tenant_id == self._tenant_id)
                    .order_by(ExportRun.started_at.desc())
                )
            )
            .scalars()
            .all()
        )
        seen_exports: set[tuple[str, str]] = set()
        for export_run in export_rows:
            # Destination-backed runs are already projected by the control
            # plane with the destination retry action. Keep this legacy
            # ledger projection only for environment-based exports so one
            # failed run cannot appear twice in Data health.
            if export_run.target_id and export_run.target_id != "legacy":
                continue
            # A worker restart can leave a terminal cancellation newer than
            # the actual failed attempt. Do not let that housekeeping record
            # hide the latest actionable failed/completed export outcome.
            if export_run.status == "cancelled":
                continue
            export_key = (
                str(export_run.exporter_type),
                str(export_run.target_id or "legacy"),
            )
            if export_key in seen_exports:
                continue
            seen_exports.add(export_key)
            if export_run.status not in {"failed", "running"}:
                continue
            is_running = export_run.status == "running"
            exporter_name = str(export_run.exporter_type).replace("-", " ")
            issues.append(
                DataHealthIssue(
                    id=f"export-run:{export_run.id}",
                    category=("export" if is_running else "failed_export"),
                    severity="warning" if is_running else "error",
                    title=(
                        f"{exporter_name.title()}-export is bezig"
                        if is_running
                        else f"{exporter_name.title()}-export mislukt"
                    ),
                    description=(
                        "Een export-run is nog actief. Controleer de run als "
                        "deze ongewoon lang blijft lopen."
                        if is_running
                        else (
                            "De laatste export-run is mislukt. Bekijk de "
                            "runhistorie en start daarna een gecontroleerde "
                            "retry."
                        )
                    ),
                    provider=str(export_run.exporter_type),
                    source="export_runs",
                    action=action(
                        "view_export" if is_running else "retry_export",
                        (
                            f"/api/v1/exporters/runs/{export_run.id}"
                            if is_running
                            else (
                                f"/api/v1/exporters/"
                                f"{export_run.exporter_type}/runs/"
                                f"{export_run.id}/retry"
                            )
                        ),
                        permissions=self._permissions,
                    ),
                    details=(
                        [
                            f"status={export_run.status}",
                            f"started_at={export_run.started_at.isoformat()}",
                        ]
                        if export_run.started_at
                        else []
                    ),
                )
            )

        account_rows = (
            await self._session.execute(
                select(
                    Account.provider_key,
                    Account.external_account_id,
                    func.count(Account.id),
                    func.min(Account.current_balance),
                    func.max(Account.current_balance),
                )
                .where(
                    Account.tenant_id == self._tenant_id,
                    Account.is_active.is_(True),
                )
                .group_by(Account.provider_key, Account.external_account_id)
                .having(func.count(Account.id) > 1)
            )
        ).all()
        for provider, external_id, count, minimum, maximum in account_rows:
            identity = f"{provider}:{external_id}"
            issues.append(
                DataHealthIssue(
                    id=f"duplicate-account:{identity}",
                    category="duplicate_accounts",
                    severity="warning",
                    title="Mogelijke dubbele account",
                    description=(
                        f"{count} actieve accounts delen dezelfde "
                        "provideridentiteit."
                    ),
                    impact_count=int(count),
                    affected_record_count=int(count),
                    provider=str(provider),
                    source="accounts",
                    evidence={
                        "total_count": int(count),
                        "detail_limit": 1,
                        "external_account_id": str(external_id),
                    },
                    action=action(
                        "view_accounts",
                        "/api/v1/accounts",
                        permissions=self._permissions,
                    ),
                )
            )
            if (
                minimum is not None
                and maximum is not None
                and minimum != maximum
            ):
                issues.append(
                    DataHealthIssue(
                        id=f"balance-conflict:{identity}",
                        category="balance_conflict",
                        severity="error",
                        title="Conflicterende saldi",
                        description=(
                            "Dezelfde provideraccount heeft verschillende "
                            "actuele "
                            "saldo's in de canonieke dataset."
                        ),
                        impact_count=int(count),
                        affected_record_count=int(count),
                        provider=str(provider),
                        source="accounts",
                        evidence={
                            "total_count": int(count),
                            "detail_limit": 1,
                            "external_account_id": str(external_id),
                        },
                        action=action(
                            "view_accounts",
                            "/api/v1/accounts",
                            permissions=self._permissions,
                        ),
                    )
                )

        import_statuses = ["failed", "partial", "incomplete", "quarantined"]
        import_scope = and_(
            ImportRun.tenant_id == self._tenant_id,
            ImportRun.status.in_(import_statuses),
        )
        import_total_result = await self._session.execute(
            select(func.count(ImportRun.id)).where(import_scope)
        )
        import_total = int(import_total_result.scalar_one() or 0)
        import_runs = (
            await self._session.execute(
                select(ImportRun)
                .where(import_scope)
                .order_by(ImportRun.created_at.desc())
                .limit(20)
            )
        ).scalars()
        issues.extend(
            DataHealthIssue(
                id=f"incomplete-import:{run.id}",
                category="incomplete_import",
                severity="error" if run.status == "failed" else "warning",
                title="Import is niet volledig verwerkt",
                description=(
                    f"Importstatus: {run.status}. Controleer de "
                    "importdetails en verwerk de bron opnieuw als dat nodig is."
                ),
                impact_count=(
                    int(run.rejected_count or 0) + int(run.skipped_count or 0)
                ),
                source="imports",
                evidence={
                    "affected_import_count": import_total,
                    "detail_limit": 20,
                },
                action=action(
                    "view_imports",
                    "/api/v1/connectors/file-uploads/runs",
                    permissions=self._permissions,
                ),
            )
            for run in import_runs
        )
        return issues

    async def _canonical_data_issues(self) -> list[DataHealthIssue]:
        """Detect source-side defects before they reach an exporter."""
        issues: list[DataHealthIssue] = []
        trade_types = ("purchase", "sale")

        incomplete_trades = (
            await self._session.execute(
                select(
                    Transaction.id,
                    Account.name,
                    Transaction.description,
                    Transaction.occurred_at,
                    func.count(Transaction.id).over().label("total_count"),
                )
                .join(Account, Account.id == Transaction.account_id)
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.transaction_type.in_(trade_types),
                    or_(
                        Transaction.quantity.is_(None),
                        Transaction.unit_price.is_(None),
                    ),
                )
                .order_by(Transaction.occurred_at.desc())
                .limit(100)
            )
        ).all()
        incomplete_total = (
            int(incomplete_trades[0][4])
            if incomplete_trades and len(tuple(incomplete_trades[0])) > 4
            else len(incomplete_trades)
        )
        if incomplete_trades:
            issues.append(
                DataHealthIssue(
                    id="incomplete-transactions:trade-fields",
                    category="incomplete_transaction",
                    severity="error",
                    title="Transacties missen handelsgegevens",
                    description=(
                        "Koop- of verkooptransacties zonder hoeveelheid of "
                        "stukprijs kunnen niet betrouwbaar worden geëxporteerd."
                    ),
                    impact_count=incomplete_total,
                    affected_record_count=incomplete_total,
                    source="transactions",
                    details=[
                        self._transaction_detail(row)
                        for row in incomplete_trades[:10]
                    ],
                    affected_transaction_ids=[
                        str(row[0]) for row in incomplete_trades
                    ],
                    evidence={
                        "total_count": incomplete_total,
                        "detail_limit": 100,
                    },
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions?status=booked",
                        permissions=self._permissions,
                    ),
                )
            )

        activity_types = (
            *trade_types,
            "deposit",
            "withdrawal",
            "dividend",
            "interest",
            "fee",
            "tax",
            "transfer",
            "split",
            "adjustment",
            "corporate_action",
        )
        cash_contract_types = (
            "deposit",
            "withdrawal",
            "dividend",
            "interest",
            "fee",
            "tax",
        )
        invalid_currency = or_(
            Transaction.currency_code.is_(None),
            ~Transaction.currency_code.op("~")("^[A-Z]{3}$"),
        )
        effective_fee_currency_invalid = or_(
            and_(
                Transaction.fee_currency_code.is_not(None),
                ~Transaction.fee_currency_code.op("~")("^[A-Z]{3}$"),
            ),
            and_(
                Transaction.fee_currency_code.is_(None),
                invalid_currency,
            ),
        )
        zero_cost_trades = (
            await self._session.execute(
                select(
                    Transaction.id,
                    Account.name,
                    Transaction.description,
                    Transaction.occurred_at,
                    Transaction.transaction_type,
                    Transaction.quantity,
                    Transaction.unit_price,
                    Transaction.amount,
                    Transaction.booked_at,
                    Transaction.security_id,
                    Transaction.currency_code,
                    Transaction.fee_amount,
                    Transaction.fee_currency_code,
                    Transaction.external_transaction_id,
                    Account.currency_code,
                    Security.currency_code,
                    Transaction.fx_rate,
                    func.count(Transaction.id)
                    .filter(
                        and_(
                            Transaction.quantity.is_not(None),
                            Transaction.unit_price.is_not(None),
                            or_(
                                Transaction.unit_price == 0,
                                Transaction.amount == 0,
                            ),
                        )
                    )
                    .over()
                    .label("zero_cost_total_count"),
                    func.count(Transaction.id)
                    .filter(Transaction.booked_at < Transaction.occurred_at)
                    .over()
                    .label("invalid_order_total_count"),
                    func.sum(
                        case(
                            (
                                and_(
                                    Transaction.transaction_type.in_(
                                        trade_types
                                    ),
                                    Transaction.security_id.is_(None),
                                ),
                                1,
                            ),
                            (
                                and_(
                                    Transaction.transaction_type.in_(
                                        cash_contract_types
                                    ),
                                    Transaction.amount.is_(None),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    )
                    .over()
                    .label("contract_incomplete_total_count"),
                    func.sum(
                        case(
                            (
                                or_(
                                    Transaction.external_transaction_id.is_(
                                        None
                                    ),
                                    func.trim(
                                        Transaction.external_transaction_id
                                    )
                                    == "",
                                ),
                                1,
                            ),
                            (
                                or_(
                                    Transaction.currency_code.is_(None),
                                    ~Transaction.currency_code.op("~")(
                                        "^[A-Z]{3}$"
                                    ),
                                ),
                                1,
                            ),
                            (
                                and_(
                                    Security.currency_code.is_not(None),
                                    Transaction.currency_code.is_not(None),
                                    Security.currency_code
                                    != Transaction.currency_code,
                                    or_(
                                        Transaction.fx_rate.is_(None),
                                        Transaction.fx_rate <= 0,
                                    ),
                                ),
                                1,
                            ),
                            (
                                and_(
                                    Transaction.transaction_type.in_(
                                        trade_types
                                    ),
                                    Transaction.quantity.is_not(None),
                                    Transaction.quantity <= 0,
                                ),
                                1,
                            ),
                            (
                                and_(
                                    Transaction.transaction_type.in_(
                                        trade_types
                                    ),
                                    Transaction.unit_price.is_not(None),
                                    Transaction.unit_price < 0,
                                ),
                                1,
                            ),
                            (
                                and_(
                                    Transaction.transaction_type.in_(
                                        ("fee", "tax")
                                    ),
                                    Transaction.fee_amount.is_not(None),
                                    Transaction.fee_amount <= 0,
                                ),
                                1,
                            ),
                            (
                                and_(
                                    Transaction.transaction_type.in_(
                                        ("fee", "tax")
                                    ),
                                    effective_fee_currency_invalid,
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    )
                    .over()
                    .label("contract_invalid_total_count"),
                    Transaction.provider_metadata_contract,
                )
                .join(Account, Account.id == Transaction.account_id)
                .outerjoin(Security, Security.id == Transaction.security_id)
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.transaction_type.in_(activity_types),
                )
                .order_by(Transaction.occurred_at.desc())
                .limit(100)
            )
        ).all()
        incomplete_ids = {str(row[0]) for row in incomplete_trades}
        semantically_zero_cost: list[tuple[object, ...]] = []
        invalid_semantics: list[tuple[str, str]] = []
        contract_incomplete: list[tuple[str, str]] = []
        contract_invalid: list[tuple[str, str, str]] = []
        zero_cost_total_count: int | None = None
        invalid_order_total_count: int | None = None
        contract_incomplete_total_count: int | None = None
        contract_invalid_total_count: int | None = None
        semantic_transactions: list[SimpleNamespace] = []
        semantic_rows: dict[str, tuple[object, ...]] = {}
        for row in zero_cost_trades:
            values = tuple(row)
            if len(values) >= 21:
                if isinstance(values[17], int):
                    zero_cost_total_count = int(values[17])
                if isinstance(values[18], int):
                    invalid_order_total_count = int(values[18])
                if isinstance(values[19], int):
                    contract_incomplete_total_count = int(values[19])
                if isinstance(values[20], int):
                    contract_invalid_total_count = int(values[20])
            if len(values) < 9:
                # Keep lightweight mock/session contracts backwards-compatible;
                # real SQL rows always contain the semantic fields below.
                semantically_zero_cost.append(values)
                continue
            transaction_values = {
                "id": values[0],
                "transaction_type": values[4],
                "quantity": values[5],
                "unit_price": values[6],
                "amount": values[7],
                "occurred_at": values[3],
                "booked_at": values[8],
            }
            if len(values) >= 17:
                transaction_values.update(
                    {
                        "security_id": values[9],
                        "currency_code": values[10],
                        "fee_amount": values[11],
                        "fee_currency_code": values[12],
                        "external_transaction_id": values[13],
                        "account_currency_code": values[14],
                        "security_currency_code": values[15],
                        "fx_rate": values[16],
                    }
                )
            if len(values) >= 22:
                transaction_values["provider_metadata_contract"] = values[21]
            semantic_transactions.append(SimpleNamespace(**transaction_values))
            semantic_rows[str(values[0])] = values
        findings_by_record: dict[str, list[str]] = {}
        semantic_findings = sorted(
            set(validate_transaction_stream(semantic_transactions)),
            key=lambda finding: (
                finding.record_id,
                finding.category,
                finding.severity,
                finding.message,
            ),
        )
        for finding in semantic_findings:
            findings_by_record.setdefault(finding.record_id, []).append(
                finding.category
            )
            if finding.category == "invalid_activity_semantics":
                if "booked_at" in finding.message:
                    invalid_semantics.append(
                        (finding.record_id, finding.message)
                    )
                else:
                    contract_invalid.append(
                        (
                            finding.record_id,
                            finding.message,
                            finding.severity,
                        )
                    )
            elif finding.category == "incomplete_transaction" and (
                "security" in finding.message
                or "cash activity" in finding.message
            ):
                contract_incomplete.append((finding.record_id, finding.message))
        for record_id, categories in findings_by_record.items():
            if (
                record_id not in incomplete_ids
                and "zero_cost_transaction" in categories
            ):
                semantically_zero_cost.append(semantic_rows[record_id])
        deduplicated_zero_cost: dict[str, tuple[object, ...]] = {}
        for row in semantically_zero_cost:
            deduplicated_zero_cost[str(row[0])] = row
        semantically_zero_cost = list(deduplicated_zero_cost.values())
        contract_incomplete = sorted(set(contract_incomplete))
        contract_invalid = sorted(set(contract_invalid))
        invalid_semantics = sorted(set(invalid_semantics))
        if semantically_zero_cost:
            issues.append(
                DataHealthIssue(
                    id="incomplete-transactions:zero-cost",
                    category="zero_cost_transaction",
                    severity="warning",
                    title="Transacties hebben een nulprijs",
                    description=(
                        "Deze transacties lijken op een corporate action of "
                        "kosteloze toekenning. Classificeer ze als Transfer In "
                        "of leg de verkrijgingsprijs vast."
                    ),
                    impact_count=(
                        zero_cost_total_count or len(semantically_zero_cost)
                    ),
                    source="transactions",
                    affected_record_count=(
                        zero_cost_total_count or len(semantically_zero_cost)
                    ),
                    evidence={
                        "total_count": (
                            zero_cost_total_count or len(semantically_zero_cost)
                        ),
                        "detail_limit": 100,
                    },
                    details=[
                        self._transaction_detail(row)
                        for row in semantically_zero_cost[:10]
                    ],
                    affected_transaction_ids=[
                        str(row[0]) for row in semantically_zero_cost
                    ],
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions?type=purchase",
                        permissions=self._permissions,
                    ),
                )
            )
        if contract_incomplete:
            digest = self._stable_issue_hash(
                ":".join(
                    f"{record_id}:{message}"
                    for record_id, message in contract_incomplete
                )
            )
            issues.append(
                DataHealthIssue(
                    id=f"incomplete-activity-contract:{digest}",
                    category="incomplete_transaction",
                    severity="error",
                    title="Activiteiten missen Wealthfolio-velden",
                    description=(
                        "Een of meer activiteiten missen een security of "
                        "cashbedrag dat Wealthfolio nodig heeft voor een "
                        "betrouwbare projectie."
                    ),
                    impact_count=(
                        contract_incomplete_total_count
                        or len(contract_incomplete)
                    ),
                    affected_record_count=(
                        contract_incomplete_total_count
                        or len(contract_incomplete)
                    ),
                    affected_transaction_ids=list(
                        dict.fromkeys(
                            record_id
                            for record_id, _message in contract_incomplete
                        )
                    )[:100],
                    details=[
                        f"{record_id}: {message}"
                        for record_id, message in contract_incomplete[:10]
                    ],
                    source="transactions",
                    blocking=True,
                    evidence={
                        "total_count": (
                            contract_incomplete_total_count
                            or len(contract_incomplete)
                        ),
                        "detail_limit": 100,
                    },
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions?status=booked",
                        permissions=self._permissions,
                    ),
                )
            )
        if contract_invalid:
            contract_severity = (
                "error"
                if any(item[2] == "error" for item in contract_invalid)
                else "warning"
            )
            digest = self._stable_issue_hash(
                ":".join(
                    f"{record_id}:{message}:{severity}"
                    for record_id, message, severity in contract_invalid
                )
            )
            issues.append(
                DataHealthIssue(
                    id=f"invalid-activity-contract:{digest}",
                    category="invalid_activity_semantics",
                    severity=contract_severity,
                    title="Activiteiten hebben ongeldige semantiek",
                    description=(
                        "Valuta- of feevelden voldoen niet aan het canonical "
                        "Wealthfolio-contract."
                    ),
                    impact_count=(
                        contract_invalid_total_count or len(contract_invalid)
                    ),
                    affected_record_count=(
                        contract_invalid_total_count or len(contract_invalid)
                    ),
                    affected_transaction_ids=list(
                        dict.fromkeys(
                            record_id
                            for (
                                record_id,
                                _message,
                                _severity,
                            ) in contract_invalid
                        )
                    )[:100],
                    details=[
                        f"{record_id}: {message}"
                        for record_id, message, _severity in contract_invalid[
                            :10
                        ]
                    ],
                    source="transactions",
                    blocking=contract_severity == "error",
                    evidence={
                        "total_count": (
                            contract_invalid_total_count
                            or len(contract_invalid)
                        ),
                        "detail_limit": 100,
                    },
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions?status=booked",
                        permissions=self._permissions,
                    ),
                )
            )
        if invalid_semantics:
            digest = self._stable_issue_hash(
                ":".join(
                    f"{record_id}:{message}"
                    for record_id, message in invalid_semantics
                )
            )
            issues.append(
                DataHealthIssue(
                    id=f"invalid-activity-semantics:{digest}",
                    category="invalid_activity_semantics",
                    severity="error",
                    title="Transactievolgorde is ongeldig",
                    description=(
                        "Een of meer activiteiten hebben een geboekt-datum "
                        "vóór de gebeurtenisdatum en zijn niet betrouwbaar "
                        "te projecteren."
                    ),
                    impact_count=len(invalid_semantics),
                    affected_record_count=len(invalid_semantics),
                    affected_transaction_ids=[
                        record_id for record_id, _message in invalid_semantics
                    ][:100],
                    evidence={
                        "total_count": (
                            invalid_order_total_count or len(invalid_semantics)
                        ),
                        "detail_limit": 100,
                    },
                    details=[
                        f"{record_id}: {message}"
                        for record_id, message in invalid_semantics[:10]
                    ],
                    source="transactions",
                    blocking=True,
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions?status=booked",
                        permissions=self._permissions,
                    ),
                )
            )

        negative_accounts = (
            await self._session.execute(
                select(Account.id, Account.name, Account.current_balance)
                .where(
                    Account.tenant_id == self._tenant_id,
                    Account.is_active.is_(True),
                    Account.current_balance < 0,
                )
                .order_by(Account.name)
            )
        ).all()
        if negative_accounts:
            issues.append(
                DataHealthIssue(
                    id="negative-balances:accounts",
                    category="negative_balance",
                    severity="warning",
                    title="Accounts hebben een negatief saldo",
                    description=(
                        "Een negatief actueel saldo wijst meestal op een "
                        "ontbrekende storting of Transfer In vóór een aankoop."
                    ),
                    impact_count=len(negative_accounts),
                    affected_record_count=len(negative_accounts),
                    source="accounts",
                    details=[
                        f"{row[1]}: {row[2]}" for row in negative_accounts
                    ],
                    evidence={
                        "total_count": len(negative_accounts),
                        "detail_limit": 10,
                    },
                    action=action(
                        "view_accounts",
                        "/api/v1/accounts",
                        permissions=self._permissions,
                    ),
                )
            )

        transfer_rows = (
            await self._session.execute(
                select(
                    Transaction.account_id,
                    Account.name,
                    Transaction.amount,
                    Transaction.currency_code,
                    func.date(Transaction.occurred_at),
                    Transaction.description,
                )
                .join(Account, Account.id == Transaction.account_id)
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.transaction_type == "transfer",
                )
                .order_by(Transaction.occurred_at)
            )
        ).all()
        transfer_findings = sorted(
            set(validate_transfer_rows(transfer_rows)),
            key=lambda finding: (
                finding.record_id,
                finding.category,
                finding.severity,
                finding.message,
            ),
        )
        for transfer_category, transfer_title, transfer_description in (
            (
                "unbalanced_transfer",
                "Transfers missen een tegenboeking",
                (
                    "Elke interne transfer moet een uitgaande én inkomende "
                    "kant hebben. Markeer externe geldstromen expliciet."
                ),
            ),
            (
                "incomplete_transaction",
                "Transfers missen bedrag of richting",
                (
                    "Een transfer moet een niet-nul bedrag hebben zodat de "
                    "richting en de tegenboeking controleerbaar zijn."
                ),
            ),
        ):
            category_findings = [
                item
                for item in transfer_findings
                if item.category == transfer_category
            ]
            if not category_findings:
                continue
            transfer_severity = (
                "error"
                if any(item.severity == "error" for item in category_findings)
                else "warning"
            )
            issues.append(
                DataHealthIssue(
                    id=(
                        f"{transfer_category.replace('_', '-')}-"
                        "transfers:canonical"
                    ),
                    category=cast("DataHealthCategory", transfer_category),
                    severity=transfer_severity,
                    title=transfer_title,
                    description=transfer_description,
                    impact_count=len(category_findings),
                    affected_record_count=len(category_findings),
                    source="transactions",
                    details=[
                        f"{finding.record_id}: {finding.message}"
                        for finding in category_findings[:10]
                    ],
                    evidence={
                        "total_count": len(category_findings),
                        "detail_limit": 10,
                    },
                    action=action(
                        "view_transactions",
                        "/api/v1/transactions?type=transfer",
                        permissions=self._permissions,
                    ),
                )
            )

        incomplete_holdings = (
            await self._session.execute(
                select(Holding.id, Account.name, Security.name)
                .add_columns(func.count(Holding.id).over().label("total_count"))
                .join(Account, Account.id == Holding.account_id)
                .join(Security, Security.id == Holding.security_id)
                .where(
                    Holding.tenant_id == self._tenant_id,
                    Holding.quantity != 0,
                    Holding.price.is_(None),
                    Holding.market_value.is_(None),
                )
                .order_by(Holding.observed_at.desc())
                .limit(100)
            )
        ).all()
        incomplete_holdings_total = (
            int(incomplete_holdings[0][3])
            if incomplete_holdings and len(tuple(incomplete_holdings[0])) > 3
            else len(incomplete_holdings)
        )
        if incomplete_holdings:
            issues.append(
                DataHealthIssue(
                    id="incomplete-holdings:valuation",
                    category="incomplete_holding",
                    severity="warning",
                    title="Posities missen waarderingsgegevens",
                    description=(
                        "Een positie mist een prijs of marktwaarde. Daardoor "
                        "kunnen portefeuillewaarde en rendement afwijken."
                    ),
                    impact_count=incomplete_holdings_total,
                    affected_record_count=incomplete_holdings_total,
                    source="holdings",
                    details=[
                        f"{row[2]} in {row[1]}"
                        for row in incomplete_holdings[:10]
                    ],
                    evidence={
                        "total_count": incomplete_holdings_total,
                        "detail_limit": 100,
                    },
                    affected_transaction_ids=[],
                    action=action(
                        "view_holdings",
                        "/api/v1/holdings",
                        permissions=self._permissions,
                    ),
                )
            )

        incomplete_securities = (
            await self._session.execute(
                select(Security.id, Security.name)
                .add_columns(
                    func.count(Security.id).over().label("total_count")
                )
                .where(
                    Security.isin.is_(None),
                    Security.ticker.is_(None),
                    or_(
                        exists(
                            select(Holding.id).where(
                                Holding.tenant_id == self._tenant_id,
                                Holding.security_id == Security.id,
                            )
                        ),
                        exists(
                            select(Transaction.id).where(
                                Transaction.tenant_id == self._tenant_id,
                                Transaction.security_id == Security.id,
                            )
                        ),
                    ),
                )
                .order_by(Security.name)
                .limit(100)
            )
        ).all()
        incomplete_securities_total = (
            int(incomplete_securities[0][2])
            if incomplete_securities
            and len(tuple(incomplete_securities[0])) > 2
            else len(incomplete_securities)
        )
        if incomplete_securities:
            issues.append(
                DataHealthIssue(
                    id="incomplete-securities:identity",
                    category="incomplete_security_identity",
                    severity="error",
                    title="Securities missen een identificatie",
                    description=(
                        "Zonder ISIN of ticker kunnen koersproviders en "
                        "exportbestemmingen de security niet betrouwbaar "
                        "herkennen."
                    ),
                    impact_count=incomplete_securities_total,
                    affected_record_count=incomplete_securities_total,
                    source="securities",
                    details=[str(row[1]) for row in incomplete_securities[:10]],
                    evidence={
                        "total_count": incomplete_securities_total,
                        "detail_limit": 100,
                    },
                    action=action(
                        "view_holdings",
                        "/api/v1/holdings",
                        permissions=self._permissions,
                    ),
                )
            )
        return issues

    @staticmethod
    def _transaction_detail(row: Iterable[object]) -> str:
        values = tuple(row)
        _transaction_id, account_name, description, occurred_at = values[:4]
        occurred_at = cast("datetime | None", occurred_at)
        date = (
            occurred_at.strftime("%Y-%m-%d")
            if occurred_at
            else "onbekende datum"
        )
        return f"{account_name} · {date} · {description or 'Transactie'}"

    async def _changed_provider_issues(
        self, sources: list[DataHealthSource]
    ) -> list[DataHealthIssue]:
        """Create recovery actions for revised provider transactions."""
        rows = (
            await self._session.execute(
                select(Transaction.provider_key, func.count(Transaction.id))
                .where(
                    Transaction.tenant_id == self._tenant_id,
                    Transaction.revision > 1,
                )
                .group_by(Transaction.provider_key)
            )
        ).all()
        issues: list[DataHealthIssue] = []
        for provider, count in rows:
            source = next(
                (item for item in sources if item.provider == str(provider)),
                None,
            )
            recovery = (
                action(
                    "sync_connection",
                    f"/api/v1/sync/connections/{source.id}/start",
                    permissions=self._permissions,
                )
                if source is not None
                else action(
                    "view_transactions",
                    "/api/v1/transactions",
                    permissions=self._permissions,
                )
            )
            issues.append(
                DataHealthIssue(
                    id=f"provider-data-changed:{provider}",
                    category="provider_data_changed",
                    severity="warning",
                    title="Providerdata is gewijzigd",
                    description=(
                        f"{count} transacties van {provider} zijn door de bron "
                        "aangepast na een eerdere synchronisatie."
                    ),
                    impact_count=int(count),
                    affected_record_count=int(count),
                    provider=str(provider),
                    source="transactions",
                    evidence={
                        "total_count": int(count),
                        "detail_limit": 1,
                    },
                    action=recovery,
                )
            )
        return issues

    @staticmethod
    def _status(
        control_status: str,
        quality_status: str,
        issues: list[DataHealthIssue] | None = None,
    ) -> DataHealthStatus:
        if control_status == "sync_failed":
            return "error"
        if control_status == "attention_required":
            return "attention_required"
        if control_status == "partial":
            return "partial"
        if quality_status == "attention_required":
            return "attention_required"
        if quality_status == "unavailable":
            return "unavailable"
        if any(issue.severity == "error" for issue in issues or []):
            return "error"
        if any(issue.severity == "warning" for issue in issues or []):
            return "attention_required"
        return "healthy"

    @staticmethod
    def _issue(issue: ControlPlaneIssue) -> DataHealthIssue:
        category = {
            "security_mapping": "unresolved_security",
            "freshness": "stale_prices",
            "export": "failed_export",
            "data_quality": "reconciliation",
        }.get(issue.category, issue.category)
        return DataHealthIssue(
            id=issue.id,
            category=category,  # type: ignore[arg-type]
            severity=issue.severity,
            title=issue.title,
            description=issue.description,
            impact_count=issue.impact_count,
            provider=issue.provider,
            source=issue.category,
            action=issue.action,
            affected_transaction_ids=issue.affected_transaction_ids,
        )
