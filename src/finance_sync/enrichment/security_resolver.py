"""SecurityResolver — resolves security identities from
connector transactions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from finance_sync.db.uow import UnitOfWork

from finance_sync.enrichment.models import ResolvedSecurity, UnresolvedSecurity
from finance_sync.models.enums import SecurityType

if TYPE_CHECKING:
    from finance_sync.enrichment.gateway import EnrichmentGateway


class SecurityResolver:
    """Resolves security identities from connector-provided identifiers.

    Takes raw transaction/holding data from connectors and:
    - Resolves canonical security identities (ISIN, FIGI, ticker)
    - Fills missing ISINs using OpenBB lookups
    - Maps Trading212 FIGI symbols to canonical securities
    - Reports unresolved securities for manual intervention
    """

    # Known Trading212 instrument prefixes that carry FIGI-like codes
    _T212_FIGI_PREFIXES = ("EQ.", "ETF.", "ADR.", "FUND.")

    def __init__(
        self,
        uow: UnitOfWork,
        gateway: EnrichmentGateway,
    ) -> None:
        self._uow = uow
        self._gateway = gateway

    # ── Public API ───────────────────────────────────────────────────────

    async def resolve_from_connector_data(
        self,
        provider_key: str,
        instrument_data: list[dict[str, Any]],
    ) -> tuple[list[ResolvedSecurity], list[UnresolvedSecurity]]:
        """Resolve security identities from raw connector instrument data.

        Args:
            provider_key: Connector provider identifier (e.g. 'trading212').
            instrument_data: List of raw instrument dicts from the connector.
                Each dict should have at minimum an identifier field
                (e.g. 'ticker', 'figi', 'isin', 'name').

        Returns:
            Tuple of (resolved_securities, unresolved_securities).
        """
        resolved: list[ResolvedSecurity] = []
        unresolved: list[UnresolvedSecurity] = []

        for item in instrument_data:
            result = await self._resolve_single(provider_key, item)
            if isinstance(result, ResolvedSecurity):
                resolved.append(result)
            else:
                unresolved.append(result)

        return resolved, unresolved

    async def resolve_by_isin(
        self,
        isin: str,
    ) -> ResolvedSecurity | UnresolvedSecurity:
        """Resolve a security by its ISIN."""
        # Check local DB first
        local = await self._find_local_by_isin(isin)
        if local is not None:
            return local

        # Try OpenBB gateway
        return await self._resolve_via_gateway(
            identifier=isin,
            identifier_type="isin",
        )

    async def resolve_by_figi(
        self,
        figi: str,
    ) -> ResolvedSecurity | UnresolvedSecurity:
        """Resolve a security by its OpenFIGI identifier."""
        local = await self._find_local_by_figi(figi)
        if local is not None:
            return local

        return await self._resolve_via_gateway(
            identifier=figi,
            identifier_type="figi",
        )

    async def resolve_by_ticker(
        self,
        ticker: str,
    ) -> ResolvedSecurity | UnresolvedSecurity:
        """Resolve a security by its ticker symbol."""
        local = await self._find_local_by_ticker(ticker)
        if local is not None:
            return local

        return await self._resolve_via_gateway(
            identifier=ticker,
            identifier_type="ticker",
        )

    # ── Internal Resolution ──────────────────────────────────────────────

    async def _resolve_single(
        self,
        provider_key: str,
        item: dict[str, Any],
    ) -> ResolvedSecurity | UnresolvedSecurity:
        """Attempt to resolve a single instrument from connector data.

        Resolution order:
        1. ISIN (if present) — highest confidence
        2. FIGI (Trading212 style) — map to canonical
        3. Ticker — low confidence, may need confirmation
        4. Name — fuzzy search / OpenBB lookup
        """
        isin = item.get("isin") or item.get("ISIN")
        if isin:
            result = await self.resolve_by_isin(isin)
            if isinstance(result, ResolvedSecurity):
                return result
            # ISIN failed — try other identifiers

        figi = item.get("figi") or item.get("FIGI")
        ticker = item.get("ticker") or item.get("Ticker") or item.get("symbol")
        name = item.get("name") or item.get("Name") or item.get("description")

        # Try FIGI
        if figi:
            result = await self.resolve_by_figi(figi)
            if isinstance(result, ResolvedSecurity):
                return result

        # Try ticker
        if ticker:
            result = await self.resolve_by_ticker(ticker)
            if isinstance(result, ResolvedSecurity):
                return ResolvedSecurity(
                    security_id=result.security_id,
                    isin=result.isin,
                    figi=figi or result.figi,
                    ticker=ticker,
                    name=result.name,
                    currency_code=result.currency_code,
                    confidence="ticker_only",
                    source=result.source,
                )

        # Fall back to name-based lookup via OpenBB
        if name:
            result = await self._resolve_via_gateway(
                identifier=name,
                identifier_type="name",
            )
            if isinstance(result, ResolvedSecurity):
                return result

        # Could not resolve
        return UnresolvedSecurity(
            identifier=(
                isin or figi or ticker or name or str(item.get("id", "unknown"))
            ),
            identifier_type=(
                "isin"
                if isin
                else "figi"
                if figi
                else "ticker"
                if ticker
                else "name"
                if name
                else "external_id"
            ),
            reason="Could not match to any known security "
            "via ISIN, FIGI, ticker, or name lookup",
            provider_key=provider_key,
        )

    async def _resolve_via_gateway(
        self,
        identifier: str,
        identifier_type: str,
    ) -> ResolvedSecurity | UnresolvedSecurity:
        """Resolve via the OpenBB enrichment gateway."""
        result = await self._gateway.resolve_security(
            identifier=identifier,
            identifier_type=identifier_type,
        )
        if result is None:
            return UnresolvedSecurity(
                identifier=identifier,
                identifier_type=identifier_type,
                reason="OpenBB gateway returned no match",
            )
        return result

    # ── Local DB lookups ─────────────────────────────────────────────────

    async def _find_local_by_isin(self, isin: str) -> ResolvedSecurity | None:
        """Check if a security with the given ISIN already exists."""
        securities = await self._uow.securities.list(
            self._uow.securities.model_class.isin == isin  # type: ignore[attr-defined]
        )
        if not securities:
            return None
        sec = securities[0]
        return ResolvedSecurity(
            security_id=sec.id,
            isin=sec.isin,
            figi=sec.figi,
            ticker=sec.ticker,
            name=sec.name,
            currency_code=sec.currency_code,
            confidence="exact",
            source="local_db",
        )

    async def _find_local_by_figi(self, figi: str) -> ResolvedSecurity | None:
        """Check if a security with the given FIGI already exists."""
        securities = await self._uow.securities.list(
            self._uow.securities.model_class.figi == figi  # type: ignore[attr-defined]
        )
        if not securities:
            return None
        sec = securities[0]
        return ResolvedSecurity(
            security_id=sec.id,
            isin=sec.isin,
            figi=sec.figi,
            ticker=sec.ticker,
            name=sec.name,
            currency_code=sec.currency_code,
            confidence="exact",
            source="local_db",
        )

    async def _find_local_by_ticker(
        self, ticker: str
    ) -> ResolvedSecurity | None:
        """Check if a security with the given ticker already exists."""
        securities = await self._uow.securities.list(
            self._uow.securities.model_class.ticker == ticker  # type: ignore[attr-defined]
        )
        if not securities:
            return None
        sec = securities[0]
        return ResolvedSecurity(
            security_id=sec.id,
            isin=sec.isin,
            figi=sec.figi,
            ticker=ticker,
            name=sec.name,
            currency_code=sec.currency_code,
            confidence="ticker_only",
            source="local_db",
        )

    # ── Trading212-specific FIGI mapping ─────────────────────────────────

    @staticmethod
    def is_trading212_figi(figi: str) -> bool:
        """Check if a FIGI identifier originates from Trading212.

        Trading212 uses FIGI-like codes prefixed with venue markers
        such as ``EQ.``, ``ETF.``, ``ADR.``, ``FUND.``.
        """
        return any(
            figi.upper().startswith(prefix)
            for prefix in SecurityResolver._T212_FIGI_PREFIXES
        )

    @staticmethod
    def strip_trading212_prefix(figi: str) -> str:
        """Strip the Trading212 venue prefix from a FIGI-like code.

        E.g. ``EQ.US0378331005`` → ``US0378331005``
        """
        for prefix in SecurityResolver._T212_FIGI_PREFIXES:
            if figi.upper().startswith(prefix):
                return figi[len(prefix) :]
        return figi

    @staticmethod
    def infer_security_type(
        ticker: str, figi: str | None = None
    ) -> SecurityType:
        """Infer the security type from a ticker or FIGI context."""
        if figi:
            upper_figi = figi.upper()
            if upper_figi.startswith("ETF."):
                return SecurityType.ETF
            if upper_figi.startswith("FUND."):
                return SecurityType.MUTUAL_FUND
            if upper_figi.startswith("ADR."):
                return SecurityType.STOCK

        # Heuristic: if ticker ends with common ETF markers
        etf_markers = [".AS", ".DE", ".L", ".PA"]
        if any(ticker.upper().endswith(m) for m in etf_markers):
            return SecurityType.ETF

        return SecurityType.STOCK
