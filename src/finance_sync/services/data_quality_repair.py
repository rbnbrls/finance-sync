"""Safe, repeatable repairs for market-data quality issues.

This service deliberately does not manufacture broker activity.  It repairs
only facts that can be verified from an instrument master or a quote source;
missing transactions, cost basis and transfers remain actionable findings.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.models.credential import Credential
from finance_sync.models.security import Security
from finance_sync.models.unresolved_security import UnresolvedSecurity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.config.settings import Settings


class DataQualityRepairService:
    """Run deterministic quality repairs for one tenant.

    The method is idempotent and safe to run from both the API and worker.
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def run(self, tenant_id: str) -> dict[str, Any]:
        identity = await self._repair_trading212_identities(tenant_id)
        quotes = await self._refresh_quotes(tenant_id)
        remaining = await self._remaining(tenant_id)
        return {
            "started_at": datetime.now(UTC).isoformat(),
            "identity": identity,
            "quotes": quotes,
            "remaining": remaining,
            "safe_repairs_only": True,
        }

    async def _repair_trading212_identities(
        self, tenant_id: str
    ) -> dict[str, int]:
        credentials = list((await self._session.execute(
            select(Credential).where(
                Credential.tenant_id == tenant_id,
                Credential.provider_key == "trading212",
                Credential.status == "active",
            )
        )).scalars())
        rows = list((await self._session.execute(
            select(UnresolvedSecurity).where(
                UnresolvedSecurity.tenant_id == tenant_id,
                UnresolvedSecurity.provider_key == "trading212",
                UnresolvedSecurity.resolved_security_id.is_(None),
            )
        )).scalars())
        fetched = updated = resolved = 0
        for credential in credentials:
            if not credential.encrypted_payload:
                continue
            from finance_sync.services.auth import decrypt_credential

            try:
                raw = decrypt_credential(
                    credential.encrypted_payload,
                    credential.nonce,
                    self._settings,
                )
                payload = json.loads(raw)
                payload = payload if isinstance(payload, dict) else {}
                connector = ConnectorRegistry().get_connector(ConnectorConfig(
                    provider_type="trading212",
                    credentials=payload,
                    options=self._options(credential),
                ))
                await connector.authenticate()
                instruments = await connector.fetch_instruments()  # type: ignore[attr-defined]
            except Exception:
                continue
            fetched += len(instruments)
            by_key = {
                key: item for item in instruments if isinstance(item, dict)
                for key in {
                    str(item.get("ticker") or item.get("symbol") or "").upper()
                }
                if key
            }
            for row in rows:
                item = by_key.get(
                    str(row.external_security_id or "").upper()
                ) or by_key.get(str(row.raw_ticker or "").upper())
                if not item:
                    continue
                row.raw_isin = (
                    str(item.get("isin") or item.get("ISIN") or "")
                    or row.raw_isin
                )
                row.raw_ticker = (
                    str(item.get("ticker") or item.get("symbol") or "")
                    or row.raw_ticker
                )
                row.raw_name = (
                    str(item.get("name") or item.get("shortName") or "")
                    or row.raw_name
                )
                row.raw_currency_code = (
                    str(item.get("currencyCode") or item.get("currency") or "")
                    or row.raw_currency_code
                )
                row.raw_metadata = json.dumps(item, sort_keys=True, default=str)
                row.resolution_method = "trading212_metadata"
                updated += 1
                if row.raw_isin:
                    candidate = await self._session.scalar(
                        select(Security).where(
                            Security.isin == row.raw_isin.upper()
                        )
                    )
                    if candidate is not None:
                        row.resolved_security_id = str(candidate.id)
                        row.resolution_method = "auto_isin"
                        resolved += 1
        return {
            "credentials": len(credentials),
            "instruments_fetched": fetched,
            "updated": updated,
            "resolved_by_isin": resolved,
        }

    async def _refresh_quotes(self, _tenant_id: str) -> dict[str, int]:
        from finance_sync.db.uow import UnitOfWork
        from finance_sync.enrichment.gateway import EnrichmentGateway
        from finance_sync.enrichment.price_store import PriceStore

        securities = list(
            (await self._session.execute(select(Security))).scalars()
        )
        gateway = EnrichmentGateway(
            settings=self._settings,
            uow=UnitOfWork(self._session),
            price_store=PriceStore(self._session, self._settings),
        )
        updated = failed = skipped = 0
        for security in securities:
            identifier = security.isin or security.ticker or security.figi
            if not identifier:
                skipped += 1
                continue
            try:
                quote = await gateway.get_latest_quote(
                    security_id=str(security.id),
                    identifier=identifier,
                    identifier_type="isin" if security.isin else "ticker",
                )
                updated += quote is not None
                failed += quote is None
            except Exception:
                failed += 1
        return {"updated": updated, "failed": failed, "skipped": skipped}

    async def _remaining(self, tenant_id: str) -> dict[str, int]:
        unresolved = await self._session.scalar(
            select(func.count())
            .select_from(UnresolvedSecurity)
            .where(
                UnresolvedSecurity.tenant_id == tenant_id,
                UnresolvedSecurity.resolved_security_id.is_(None),
            )
        )
        return {"unresolved_securities": int(unresolved or 0)}

    @staticmethod
    def _options(credential: Credential) -> dict[str, Any]:
        try:
            value = json.loads(credential.description or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return {key: value for key, value in value.items() if key != "_label"}
