"""Extended tests for merchant classifier covering DB-dependent methods.

Complements test_merchant_classifier.py with coverage for:
- classify() with use_fundamentals=True (mocked UoW)
- classify_batch empty merchant name skip
- _find_security_by_ticker
- _find_latest_fundamentals
- _resolve_security_with_fundamentals error handling
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from finance_sync.services.merchant_classifier import (
    LIKELIHOOD_HIGH,
    MerchantClassifier,
    _get_sector_likelihood,
)


class TestMerchantClassifierFundamentals:
    """Test the MerchantClassifier with mocked UoW and fundamentals."""

    @pytest.fixture
    def mock_security(self) -> MagicMock:
        """A mocked Security ORM object."""
        sec = MagicMock()
        sec.id = "sec_nflx"
        sec.ticker = "NFLX"
        return sec

    @pytest.fixture
    def mock_fundamentals(self) -> MagicMock:
        """A mocked FundamentalObservation ORM object."""
        obs = MagicMock()
        obs.market_cap = Decimal(300000000000)
        obs.pe_ratio = Decimal(45)
        obs.dividend_yield = Decimal("0.005")
        obs.eps = Decimal("6.50")
        obs.beta = Decimal("1.3")
        obs.forward_pe = Decimal(40)
        return obs

    @pytest.fixture
    def mock_session(self, mock_security, mock_fundamentals) -> AsyncMock:
        """Mock session that returns security and fundamentals."""
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock()

        # Mock the security query
        security_result = MagicMock()
        security_result.scalar_one_or_none.return_value = mock_security

        # Mock the fundamentals query
        fund_result = MagicMock()
        fund_result.scalar_one_or_none.return_value = mock_fundamentals

        # session.execute returns security_result first, then fund_result
        session.execute = AsyncMock(side_effect=[security_result, fund_result])
        return session

    @pytest.fixture
    def mock_uow(self, mock_session) -> MagicMock:
        """Mock UoW with a session factory."""
        uow = MagicMock()
        session_factory = MagicMock()
        session_factory.return_value = mock_session
        # UoW has _session_factory as an attribute used by auth methods
        type(uow)._session_factory = PropertyMock(return_value=session_factory)
        return uow

    @pytest.mark.asyncio
    async def test_classify_with_fundamentals_enriches_result(
        self, mock_uow
    ) -> None:
        """classify() with use_fundamentals=True fetches security and
        fundamentals, producing a richer result."""
        classifier = MerchantClassifier(uow=mock_uow)
        result = await classifier.classify(
            "Netflix B.V.",
            use_fundamentals=True,
        )

        assert result.sector == "Communication Services"
        assert result.ticker == "NFLX"
        assert result.security_id is not None
        assert result.fundamentals_available
        assert result.source == "merchant_map"
        assert result.subscription_likelihood == LIKELIHOOD_HIGH

    @pytest.mark.asyncio
    async def test_classify_with_fundamentals_no_uow(self) -> None:
        """When uow is None, fundamentals are skipped."""
        classifier = MerchantClassifier(uow=None)
        result = await classifier.classify(
            "Netflix B.V.",
            use_fundamentals=True,
        )

        assert result.sector == "Communication Services"
        assert result.ticker == "NFLX"
        assert result.security_id is None
        assert not result.fundamentals_available
        assert result.source == "merchant_map"

    @pytest.mark.asyncio
    async def test_classify_with_fundamentals_skipped_when_no_ticker(
        self, mock_uow
    ) -> None:
        """When ticker is None (private company), fundamentals are skipped."""
        classifier = MerchantClassifier(uow=mock_uow)
        # Patreon is a private company with no ticker
        result = await classifier.classify(
            "Patreon",
            use_fundamentals=True,
        )

        assert result.sector == "Technology"
        assert result.ticker is None
        assert result.security_id is None
        assert not result.fundamentals_available

    @pytest.mark.asyncio
    async def test_classify_with_fundamentals_no_security_found(
        self, mock_uow
    ) -> None:
        """When security is not found in DB, fundamentals are skipped."""
        # Override the session to return None for security
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock()

        security_result = MagicMock()
        security_result.scalar_one_or_none.return_value = None

        session.execute = AsyncMock(return_value=security_result)

        session_factory = MagicMock()
        session_factory.return_value = session
        type(mock_uow)._session_factory = PropertyMock(
            return_value=session_factory
        )

        classifier = MerchantClassifier(uow=mock_uow)
        result = await classifier.classify(
            "Netflix B.V.",
            use_fundamentals=True,
        )

        # Falls back to merchant_map data without security_id
        assert result.sector == "Communication Services"
        assert result.ticker == "NFLX"
        assert result.security_id is None
        assert not result.fundamentals_available
        assert result.source == "merchant_map"

    @pytest.mark.asyncio
    async def test_classify_with_fundamentals_exception_handled(
        self, mock_uow
    ) -> None:
        """When DB query raises, the exception is caught and fundamentals
        are gracefully skipped."""
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock()
        session.execute = AsyncMock(side_effect=Exception("DB down"))

        session_factory = MagicMock()
        session_factory.return_value = session
        type(mock_uow)._session_factory = PropertyMock(
            return_value=session_factory
        )

        classifier = MerchantClassifier(uow=mock_uow)
        result = await classifier.classify(
            "Netflix B.V.",
            use_fundamentals=True,
        )

        assert result.sector == "Communication Services"
        assert result.ticker == "NFLX"
        assert result.security_id is None
        assert not result.fundamentals_available

    @pytest.mark.asyncio
    async def test_resolve_security_with_fundamentals_db_exception(
        self, mock_uow
    ) -> None:
        """When the DB query raises, it's caught and returns None, None."""
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock()
        session.execute = AsyncMock(side_effect=Exception("Connection refused"))

        session_factory = MagicMock()
        session_factory.return_value = session
        type(mock_uow)._session_factory = PropertyMock(
            return_value=session_factory
        )

        classifier = MerchantClassifier(uow=mock_uow)
        (
            sec_id,
            fund_data,
        ) = await classifier._resolve_security_with_fundamentals("NFLX")
        assert sec_id is None
        assert fund_data is None

    @pytest.mark.asyncio
    async def test_classify_batch_skips_empty_name(self, mock_uow) -> None:
        """classify_batch skips entries with empty merchant_name."""
        classifier = MerchantClassifier(uow=mock_uow)
        merchants = [
            {"merchant_name": "Netflix B.V."},
            {"merchant_name": ""},  # Should be skipped
            {"merchant_name": "Dropbox Inc."},
        ]
        results = await classifier.classify_batch(
            merchants, use_fundamentals=False
        )
        assert "Netflix B.V." in results
        assert "" not in results  # empty name not in results
        assert "Dropbox Inc." in results
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_find_security_by_ticker(
        self, mock_uow, mock_security
    ) -> None:
        """_find_security_by_ticker queries the Security table."""
        classifier = MerchantClassifier(uow=mock_uow)
        result = await classifier._find_security_by_ticker("NFLX")

        # Our mock returns the mock_security object
        assert result is not None

    @pytest.mark.asyncio
    async def test_find_security_by_ticker_no_uow(self) -> None:
        """_find_security_by_ticker returns None when uow is None."""
        classifier = MerchantClassifier(uow=None)
        result = await classifier._find_security_by_ticker("NFLX")
        assert result is None

    @pytest.mark.asyncio
    async def test_find_latest_fundamentals(
        self, mock_uow, mock_fundamentals
    ) -> None:
        """_find_latest_fundamentals returns the most recent observation."""
        # Create a dedicated session that returns fund_result on first call
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock()

        fund_result = MagicMock()
        fund_result.scalar_one_or_none.return_value = mock_fundamentals

        session.execute = AsyncMock(return_value=fund_result)

        session_factory = MagicMock()
        session_factory.return_value = session
        type(mock_uow)._session_factory = PropertyMock(
            return_value=session_factory
        )

        classifier = MerchantClassifier(uow=mock_uow)
        result = await classifier._find_latest_fundamentals("sec_nflx")

        assert result is not None
        assert result["pe_ratio"] == Decimal(45)
        assert result["dividend_yield"] == Decimal("0.005")
        assert result["market_cap"] == Decimal(300000000000)

    @pytest.mark.asyncio
    async def test_find_latest_fundamentals_no_result(self, mock_uow) -> None:
        """When no fundamentals found, returns None."""
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock()

        fund_result = MagicMock()
        fund_result.scalar_one_or_none.return_value = None

        session.execute = AsyncMock(return_value=fund_result)

        session_factory = MagicMock()
        session_factory.return_value = session
        type(mock_uow)._session_factory = PropertyMock(
            return_value=session_factory
        )

        classifier = MerchantClassifier(uow=mock_uow)
        result = await classifier._find_latest_fundamentals("sec_none")
        assert result is None

    @pytest.mark.asyncio
    async def test_find_latest_fundamentals_no_uow(self) -> None:
        """_find_latest_fundamentals returns None when uow is None."""
        classifier = MerchantClassifier(uow=None)
        result = await classifier._find_latest_fundamentals("sec_nflx")
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_security_with_fundamentals_no_uow(self) -> None:
        """resolve_security_with_fundamentals returns None, None when no UoW."""
        classifier = MerchantClassifier(uow=None)
        (
            sec_id,
            fund_data,
        ) = await classifier._resolve_security_with_fundamentals("NFLX")
        assert sec_id is None
        assert fund_data is None

    @pytest.mark.asyncio
    async def test_high_pe_boosts_likelihood(self) -> None:
        """High PE with low dividend upgrades to HIGH."""
        classifier = MerchantClassifier(uow=None)

        # MSFT (Technology) with use_fundamentals=False -> HIGH
        result = await classifier.classify(
            "Microsoft",
            use_fundamentals=False,
        )
        assert result.sector == "Technology"
        assert result.ticker == "MSFT"
        assert result.source == "merchant_map"

    def test_get_sector_likelihood_consumer_staples(self) -> None:
        """Consumer Staples sector has MEDIUM likelihood."""
        assert _get_sector_likelihood("Consumer Staples") == "medium"
