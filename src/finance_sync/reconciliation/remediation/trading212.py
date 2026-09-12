"""Trading212 remediation strategy for unresolved instrument metadata."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select

from finance_sync.connectors.models import ConnectorConfig
from finance_sync.connectors.registry import ConnectorRegistry
from finance_sync.models.credential import Credential
from finance_sync.models.security import Security
from finance_sync.models.unresolved_security import UnresolvedSecurity
from finance_sync.reconciliation.remediation.verification import (
    VerificationResult,
)
from finance_sync.services.auth import decrypt_credential

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class Trading212InstrumentMetadataStrategy:
    key = "trading212_instrument_metadata"
    endpoint_family = "metadata"
    quota_cost = 1

    def supports(self, item: Any) -> bool:
        return (
            str(item.provider_key).lower() == "trading212"
            and str(item.remediation_strategy) == self.key
            and bool(item.affected_entity_id)
        )

    def __init__(self, session: AsyncSession, settings: Any) -> None:
        self.session = session
        self.settings = settings

    async def execute(self, item: Any, connector: Any = None) -> None:
        del connector
        row = await self.session.get(
            UnresolvedSecurity, item.affected_entity_id
        )
        if (
            row is not None
            and getattr(row, "tenant_id", item.tenant_id) != item.tenant_id
        ):
            row = None
        if row is None or row.resolved_security_id is not None:
            return
        credential = await self._credential(item, row)
        if credential is None:
            message = "active Trading212 connection not found"
            raise RuntimeError(message)
        payload = json.loads(
            decrypt_credential(
                credential.encrypted_payload, credential.nonce, self.settings
            )
        )
        options = json.loads(credential.description or "{}")
        options.pop("_label", None)
        instance = ConnectorRegistry().get_connector(
            ConnectorConfig(
                provider_type="trading212",
                credentials={str(k): str(v) for k, v in payload.items()},
                options=options,
            )
        )
        await instance.authenticate()
        instruments = await cast("Any", instance).fetch_instruments()
        wanted = str(row.external_security_id or row.raw_ticker or "").upper()
        match = next(
            (
                value
                for value in instruments
                if str(value.get("ticker") or value.get("symbol") or "").upper()
                == wanted
            ),
            None,
        )
        if match is None:
            return
        row.raw_isin = (
            str(match.get("isin") or match.get("ISIN") or "") or row.raw_isin
        )
        row.raw_ticker = (
            str(match.get("ticker") or match.get("symbol") or "")
            or row.raw_ticker
        )
        row.raw_name = (
            str(match.get("name") or match.get("shortName") or "")
            or row.raw_name
        )
        row.raw_currency_code = (
            str(match.get("currencyCode") or match.get("currency") or "")
            or row.raw_currency_code
        )
        row.raw_metadata = json.dumps(match, sort_keys=True, default=str)
        row.resolution_method = "trading212_metadata"
        if row.raw_isin:
            security = await self.session.scalar(
                select(Security).where(Security.isin == row.raw_isin.upper())
            )
            if security is not None:
                row.resolved_security_id = str(security.id)
                row.resolution_method = "auto_isin"
        await self.session.flush()

    async def verify(self, item: Any) -> VerificationResult:
        row = await self.session.get(
            UnresolvedSecurity, item.affected_entity_id
        )
        if (
            row is not None
            and getattr(row, "tenant_id", item.tenant_id) != item.tenant_id
        ):
            return VerificationResult(
                False, "unresolved security is outside tenant scope"
            )
        resolved = row is None or row.resolved_security_id is not None
        return VerificationResult(
            resolved,
            "unresolved security resolved"
            if resolved
            else "unresolved security remains",
        )

    async def execute_batch(
        self, items: list[Any], connector: Any = None
    ) -> dict[str, str]:
        """Fetch one instrument master and map every compatible item."""
        del connector
        if not items:
            return {}
        first = await self.session.get(
            UnresolvedSecurity, items[0].affected_entity_id
        )
        if (
            first is not None
            and getattr(first, "tenant_id", items[0].tenant_id)
            != items[0].tenant_id
        ):
            first = None
        if first is None:
            return {str(item.id): "success" for item in items}
        credential = await self._credential(items[0], first)
        if credential is None:
            return {str(item.id): "retry" for item in items}
        payload = json.loads(
            decrypt_credential(
                credential.encrypted_payload, credential.nonce, self.settings
            )
        )
        options = json.loads(credential.description or "{}")
        options.pop("_label", None)
        instance = ConnectorRegistry().get_connector(
            ConnectorConfig(
                provider_type="trading212",
                credentials={str(k): str(v) for k, v in payload.items()},
                options=options,
            )
        )
        await instance.authenticate()
        instruments = await cast("Any", instance).fetch_instruments()
        by_key = {
            str(value.get("ticker") or value.get("symbol") or "").upper(): value
            for value in instruments
        }
        outcomes: dict[str, str] = {}
        for item in items:
            row = await self.session.get(
                UnresolvedSecurity, item.affected_entity_id
            )
            if (
                row is not None
                and getattr(row, "tenant_id", item.tenant_id) != item.tenant_id
            ):
                row = None
            wanted = (
                str(row.external_security_id or row.raw_ticker or "").upper()
                if row is not None
                else ""
            )
            match = by_key.get(wanted)
            if row is None or match is None:
                outcomes[str(item.id)] = "retry"
                continue
            row.raw_isin = (
                str(match.get("isin") or match.get("ISIN") or "")
                or row.raw_isin
            )
            row.raw_ticker = (
                str(match.get("ticker") or match.get("symbol") or "")
                or row.raw_ticker
            )
            row.raw_name = (
                str(match.get("name") or match.get("shortName") or "")
                or row.raw_name
            )
            row.raw_metadata = json.dumps(match, sort_keys=True, default=str)
            row.resolution_method = "trading212_metadata"
            if row.raw_isin:
                security = await self.session.scalar(
                    select(Security).where(
                        Security.isin == row.raw_isin.upper()
                    )
                )
                if security is not None:
                    row.resolved_security_id = str(security.id)
                    row.resolution_method = "auto_isin"
            outcomes[str(item.id)] = "success"
        await self.session.flush()
        return outcomes

    async def _credential(
        self, item: Any, row: UnresolvedSecurity
    ) -> Credential | None:
        connection_id = item.connection_id
        stmt = select(Credential).where(
            Credential.tenant_id == item.tenant_id,
            Credential.provider_key == row.provider_key,
            Credential.status == "active",
        )
        if connection_id is not None:
            stmt = stmt.where(Credential.id == connection_id)
        return (await self.session.execute(stmt)).scalars().first()
