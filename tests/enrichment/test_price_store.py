"""Tests for the PriceStore service."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from finance_sync.enrichment.price_store import PriceStore
from finance_sync.models.security_price import SecurityPrice


class TestPriceStoreUnit:
    """Unit tests for PriceStore with mocked session."""

    @pytest.fixture
    def mock_session(self):
        return AsyncMock()

    @pytest.fixture
    def mock_settings(self):
        settings = MagicMock()
        settings.price_store_keep_minute_days = 30
        settings.price_store_keep_hour_days = 90
        settings.price_store_keep_daily_forever = True
        return settings

    @pytest.fixture
    def store(self, mock_session, mock_settings):
        return PriceStore(session=mock_session, settings=mock_settings)

    def _make_obs(self, **kwargs):
        """Helper to create a mock PriceObservation."""
        obs = MagicMock()
        obs.security_id = kwargs.get("security_id", "sec_1")
        obs.timestamp = kwargs.get(
            "timestamp", datetime(2025, 6, 15, 20, 0, 0, tzinfo=UTC)
        )
        obs.price_open = kwargs.get("price_open", Decimal("100.00"))
        obs.price_high = kwargs.get("price_high", Decimal("105.00"))
        obs.price_low = kwargs.get("price_low", Decimal("99.00"))
        obs.price_close = kwargs.get("price_close", Decimal("102.50"))
        obs.volume = kwargs.get("volume", Decimal(1000000))
        obs.source = kwargs.get("source", "openbb")
        obs.interval = kwargs.get("interval", "1d")
        obs.currency_code = kwargs.get("currency_code", "USD")
        return obs

    # ── Store tests ────────────────────────────────────────────────────

    async def test_store_single_observation(self, store, mock_session) -> None:
        """Store a single price observation."""
        # Mock _find_existing to return None (no duplicate)
        store._find_existing = AsyncMock(return_value=None)

        obs = self._make_obs()
        inserted = await store.store_prices([obs])

        assert inserted == 1
        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()

    async def test_store_deduplicates(self, store, mock_session) -> None:
        """Duplicate observations are not inserted."""
        existing = SecurityPrice(
            security_id="sec_1",
            timestamp=datetime(2025, 6, 15, 20, 0, 0, tzinfo=UTC),
            source="openbb",
            interval="1d",
            currency_code="USD",
        )
        store._find_existing = AsyncMock(return_value=existing)

        obs = self._make_obs()
        inserted = await store.store_prices([obs])

        assert inserted == 0
        mock_session.add.assert_not_called()

    async def test_store_empty_list(self, store, mock_session) -> None:
        """Storing an empty list is a no-op."""
        inserted = await store.store_prices([])
        assert inserted == 0
        mock_session.add.assert_not_called()

    # ── Query tests ────────────────────────────────────────────────────

    async def test_get_latest_price_found(self, store, mock_session) -> None:
        """get_latest_price returns most recent observation."""
        mock_result = MagicMock()
        mock_row = SecurityPrice(
            security_id="sec_1",
            timestamp=datetime(2025, 6, 15, 20, 0, 0, tzinfo=UTC),
            price_close=Decimal("110.00"),
            source="openbb",
            interval="1d",
            currency_code="USD",
        )
        mock_result.scalar_one_or_none.return_value = mock_row
        mock_session.execute.return_value = mock_result

        result = await store.get_latest_price("sec_1")
        assert result is not None
        assert result.price_close == Decimal("110.00")
        mock_session.execute.assert_awaited_once()

    async def test_get_latest_price_none(self, store, mock_session) -> None:
        """get_latest_price returns None when no data."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await store.get_latest_price("sec_nonexistent")
        assert result is None

    async def test_get_price_history(self, store, mock_session) -> None:
        """get_price_history returns observations."""
        mock_result = MagicMock()
        mock_row = SecurityPrice(
            security_id="sec_1",
            timestamp=datetime(2025, 6, 15, 20, 0, 0, tzinfo=UTC),
            price_close=Decimal("110.00"),
            source="openbb",
            interval="1d",
            currency_code="USD",
        )
        mock_result.scalars.return_value.all.return_value = [mock_row]
        mock_session.execute.return_value = mock_result

        result = await store.get_price_history("sec_1", limit=10)
        assert len(result) == 1
        assert result[0].price_close == Decimal("110.00")

    async def test_get_price_history_empty(self, store, mock_session) -> None:
        """get_price_history returns empty list when no data."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        result = await store.get_price_history("sec_nonexistent")
        assert result == []

    async def test_has_prices_true(self, store, mock_session) -> None:
        """has_prices returns True when data exists."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 3
        mock_session.execute.return_value = mock_result

        assert await store.has_prices("sec_1")

    async def test_has_prices_false(self, store, mock_session) -> None:
        """has_prices returns False when no data."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_session.execute.return_value = mock_result

        assert not await store.has_prices("sec_nonexistent")

    async def test_count_total_prices(self, store, mock_session) -> None:
        """count_total_prices returns correct count."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 42
        mock_session.execute.return_value = mock_result

        count = await store.count_total_prices()
        assert count == 42

    async def test_count_securities_with_prices(
        self, store, mock_session
    ) -> None:
        """count_securities_with_prices returns distinct count."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 5
        mock_session.execute.return_value = mock_result

        count = await store.count_securities_with_prices()
        assert count == 5

    # ── Pruning tests ──────────────────────────────────────────────────

    async def test_prune_intraday_data(self, store, mock_session) -> None:
        """prune_intraday_data deletes old intraday rows."""
        mock_session.execute.return_value.rowcount = 10

        count = await store.prune_intraday_data()
        assert count == 10
        mock_session.execute.assert_awaited_once()

    async def test_prune_hourly_data(self, store, mock_session) -> None:
        """prune_hourly_data deletes old hourly rows."""
        mock_session.execute.return_value.rowcount = 5

        count = await store.prune_hourly_data()
        assert count == 5
        mock_session.execute.assert_awaited_once()

    # ── Utility tests ──────────────────────────────────────────────────

    def test_to_observation(self) -> None:
        """_to_observation converts ORM to DTO correctly."""
        row = SecurityPrice(
            security_id="sec_1",
            timestamp=datetime(2025, 6, 15, 20, 0, 0, tzinfo=UTC),
            price_close=Decimal("100.00"),
            source="openbb",
            interval="1d",
            currency_code="USD",
        )
        obs = PriceStore._to_observation(row)
        assert obs.security_id == "sec_1"
        assert obs.price_close == Decimal("100.00")
        assert obs.source == "openbb"
        assert obs.interval == "1d"

    def test_to_observation_none_values(self) -> None:
        """_to_observation handles None numeric fields."""
        row = SecurityPrice(
            security_id="sec_1",
            timestamp=datetime(2025, 6, 15, 20, 0, 0, tzinfo=UTC),
            price_close=None,
            source="openbb",
            interval="1d",
            currency_code="USD",
        )
        obs = PriceStore._to_observation(row)
        assert obs.price_open is None
        assert obs.price_close is None
        assert obs.volume is None
