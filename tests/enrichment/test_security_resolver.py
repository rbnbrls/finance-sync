"""Tests for the SecurityResolver service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from finance_sync.enrichment.models import ResolvedSecurity, UnresolvedSecurity
from finance_sync.enrichment.security_resolver import SecurityResolver
from finance_sync.models.enums import SecurityType


class TestSecurityResolver:
    """Unit tests for SecurityResolver."""

    @pytest.fixture
    def mock_gateway(self):
        return AsyncMock()

    @pytest.fixture
    def mock_uow(self):
        uow = MagicMock()
        uow.securities = AsyncMock()
        return uow

    @pytest.fixture
    def resolver(self, mock_uow, mock_gateway):
        return SecurityResolver(uow=mock_uow, gateway=mock_gateway)

    # ── FIGI helpers ─────────────────────────────────────────────────

    def test_is_trading212_figi_true(self) -> None:
        assert SecurityResolver.is_trading212_figi("EQ.US0378331005")
        assert SecurityResolver.is_trading212_figi("ETF.IE00B4L5Y983")
        assert SecurityResolver.is_trading212_figi("ADR.US1234567890")
        assert SecurityResolver.is_trading212_figi("FUND.GB1234567890")

    def test_is_trading212_figi_false(self) -> None:
        assert not SecurityResolver.is_trading212_figi("BBG000B9XRY4")
        assert not SecurityResolver.is_trading212_figi("US0378331005")

    def test_strip_trading212_prefix(self) -> None:
        assert (
            SecurityResolver.strip_trading212_prefix("EQ.US0378331005")
            == "US0378331005"
        )
        assert (
            SecurityResolver.strip_trading212_prefix("ETF.IE00B4L5Y983")
            == "IE00B4L5Y983"
        )
        assert (
            SecurityResolver.strip_trading212_prefix("BBG000B9XRY4")
            == "BBG000B9XRY4"
        )
        assert (
            SecurityResolver.strip_trading212_prefix("US0378331005")
            == "US0378331005"
        )

    def test_infer_security_type_from_figi(self) -> None:
        assert (
            SecurityResolver.infer_security_type("AAPL", figi="EQ.US0378331005")
            == SecurityType.STOCK
        )
        assert (
            SecurityResolver.infer_security_type(
                "VWRL", figi="ETF.IE00B4L5Y983"
            )
            == SecurityType.ETF
        )
        assert (
            SecurityResolver.infer_security_type("FUNDX", figi="FUND.GB1234")
            == SecurityType.MUTUAL_FUND
        )

    def test_infer_security_type_default(self) -> None:
        assert (
            SecurityResolver.infer_security_type("AAPL") == SecurityType.STOCK
        )

    # ── Local lookups ────────────────────────────────────────────────

    async def test_resolve_by_isin_local_hit(self, resolver, mock_uow) -> None:
        """ISIN that exists locally returns a resolved security."""
        mock_security = MagicMock()
        mock_security.id = "sec_1"
        mock_security.isin = "US0378331005"
        mock_security.figi = None
        mock_security.ticker = "AAPL"
        mock_security.name = "Apple Inc."
        mock_security.currency_code = "USD"

        mock_uow.securities.list = AsyncMock(return_value=[mock_security])

        result = await resolver.resolve_by_isin("US0378331005")
        assert isinstance(result, ResolvedSecurity)
        assert result.isin == "US0378331005"
        assert result.name == "Apple Inc."
        assert result.confidence == "exact"

    async def test_resolve_by_isin_local_miss_gateway_hit(
        self, resolver, mock_uow, mock_gateway
    ) -> None:
        """ISIN not found locally but resolved by gateway."""
        mock_uow.securities.list = AsyncMock(return_value=[])
        mock_gateway.resolve_security.return_value = ResolvedSecurity(
            security_id="sec_new",
            isin="US0378331005",
            ticker="AAPL",
            name="Apple Inc.",
            currency_code="USD",
            confidence="exact",
            source="openbb",
        )

        result = await resolver.resolve_by_isin("US0378331005")
        assert isinstance(result, ResolvedSecurity)
        assert result.isin == "US0378331005"
        assert result.source == "openbb"

    async def test_resolve_by_isin_no_match(
        self, resolver, mock_uow, mock_gateway
    ) -> None:
        """ISIN not found locally or by gateway returns unresolved."""
        mock_uow.securities.list = AsyncMock(return_value=[])
        mock_gateway.resolve_security.return_value = None

        result = await resolver.resolve_by_isin("US0000000000")
        assert isinstance(result, UnresolvedSecurity)
        assert "US0000000000" in result.identifier

    # ── Full resolution from connector data ──────────────────────────

    async def test_resolve_from_connector_data_isin(
        self, resolver, mock_uow
    ) -> None:
        """Resolution with ISIN finds the security."""
        mock_security = MagicMock()
        mock_security.id = "sec_1"
        mock_security.isin = "US0378331005"
        mock_security.figi = None
        mock_security.ticker = "AAPL"
        mock_security.name = "Apple Inc."
        mock_security.currency_code = "USD"
        mock_uow.securities.list = AsyncMock(return_value=[mock_security])

        data = [
            {
                "isin": "US0378331005",
                "ticker": "AAPL",
                "name": "Apple Inc.",
            }
        ]
        resolved, unresolved = await resolver.resolve_from_connector_data(
            provider_key="trading212", instrument_data=data
        )
        assert len(resolved) == 1
        assert len(unresolved) == 0
        assert resolved[0].isin == "US0378331005"

    async def test_resolve_from_connector_data_figi(
        self, resolver, mock_uow
    ) -> None:
        """Resolution with FIGI uses the FIGI lookup path."""
        mock_security = MagicMock()
        mock_security.id = "sec_1"
        mock_security.isin = None
        mock_security.figi = "BBG000B9XRY4"
        mock_security.ticker = "AAPL"
        mock_security.name = "Apple Inc."
        mock_security.currency_code = "USD"
        mock_uow.securities.list = AsyncMock(return_value=[mock_security])

        data = [
            {
                "figi": "BBG000B9XRY4",
                "ticker": "AAPL",
            }
        ]
        resolved, _unresolved = await resolver.resolve_from_connector_data(
            provider_key="trading212", instrument_data=data
        )
        assert len(resolved) == 1
        assert resolved[0].figi == "BBG000B9XRY4"

    async def test_resolve_from_connector_data_unresolved(
        self, resolver, mock_uow, mock_gateway
    ) -> None:
        """Unknown instrument returns as unresolved."""
        mock_uow.securities.list = AsyncMock(return_value=[])
        mock_gateway.resolve_security.return_value = None

        data = [
            {
                "ticker": "UNKN123",
                "name": "Unknown Corp",
            }
        ]
        resolved, unresolved = await resolver.resolve_from_connector_data(
            provider_key="trading212", instrument_data=data
        )
        assert len(resolved) == 0
        assert len(unresolved) == 1
        assert unresolved[0].identifier == "UNKN123"

    async def test_resolve_from_empty_data(self, resolver) -> None:
        """Empty instrument data returns empty results."""
        resolved, unresolved = await resolver.resolve_from_connector_data(
            provider_key="trading212", instrument_data=[]
        )
        assert len(resolved) == 0
        assert len(unresolved) == 0
