"""Tests for the IdentityResolutionService and cleansing rules."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from finance_sync.enrichment.models import ResolvedSecurity
from finance_sync.enrichment.models import UnresolvedSecurity as UnresolvedDTO
from finance_sync.identity import ResolutionPipelineResult
from finance_sync.identity.resolver import (
    IdentityResolutionService,
    _name_similarity,
    apply_all_cleansing,
    cleanse_currency_code,
    cleanse_name,
    cleanse_ticker,
)
from finance_sync.models.unresolved_security import UnresolvedSecurity

# ── Cleansing rule tests ──────────────────────────────────────────────


class TestCleanseCurrencyCode:
    """Tests for currency code normalisation."""

    def test_normalises_to_uppercase(self) -> None:
        assert cleanse_currency_code("eur") == "EUR"

    def test_keeps_uppercase(self) -> None:
        assert cleanse_currency_code("USD") == "USD"

    def test_strips_whitespace(self) -> None:
        assert cleanse_currency_code("  gbp ") == "GBP"

    def test_rejects_invalid_length(self) -> None:
        assert cleanse_currency_code("EU") is None
        assert cleanse_currency_code("EURO") is None

    def test_rejects_non_alpha(self) -> None:
        assert cleanse_currency_code("123") is None

    def test_handles_none(self) -> None:
        assert cleanse_currency_code(None) is None

    def test_handles_empty_string(self) -> None:
        assert cleanse_currency_code("") is None


class TestCleanseName:
    """Tests for instrument name normalisation."""

    def test_strips_whitespace(self) -> None:
        assert cleanse_name("  Apple Inc.  ") == "Apple Inc."

    def test_collapses_multiple_spaces(self) -> None:
        assert cleanse_name("Apple   Inc.") == "Apple Inc."

    def test_strips_depositary_suffix(self) -> None:
        assert cleanse_name("Apple Inc. - Depository Receipt") == "Apple Inc."

    def test_strips_registered_shares_suffix(self) -> None:
        assert cleanse_name("Novartis AG - Registered Shares") == "Novartis AG"

    def test_strips_common_stock_suffix(self) -> None:
        assert cleanse_name("Microsoft Corp - Common Stock") == "Microsoft Corp"

    def test_handles_none(self) -> None:
        assert cleanse_name(None) is None

    def test_handles_empty(self) -> None:
        assert cleanse_name("") is None

    def test_only_one_suffix_removed(self) -> None:
        # "Apple Inc. - Depository Receipt - Common Stock" ends with
        # " - Common Stock", so only that suffix is stripped.
        name = "Apple Inc. - Depository Receipt - Common Stock"
        result = cleanse_name(name)
        assert result is not None
        assert "Common Stock" not in result
        assert "Depository Receipt" in result  # only last suffix stripped


class TestCleanseTicker:
    """Tests for ticker normalisation."""

    def test_uppercases(self) -> None:
        assert cleanse_ticker("aapl") == "AAPL"

    def test_strips_whitespace(self) -> None:
        assert cleanse_ticker("  aapl ") == "AAPL"

    def test_handles_none(self) -> None:
        assert cleanse_ticker(None) is None

    def test_handles_empty(self) -> None:
        assert cleanse_ticker("") is None


class TestApplyAllCleansing:
    """Tests for the full cleansing pipeline."""

    def test_cleanses_all_fields(self) -> None:
        instrument = {
            "isin": "US0378331005",
            "ticker": "  aapl  ",
            "name": "Apple   Inc. - Common Stock",
            "currency_code": "usd",
            "description": "Apple Inc.",
        }
        result = apply_all_cleansing(instrument)

        assert result["isin"] == "US0378331005"  # unchanged
        assert result["ticker"] == "AAPL"
        assert result["name"] == "Apple Inc."
        assert result["currency_code"] == "USD"
        assert result["original_ticker"] == "  aapl  "
        assert result["original_currency_code"] == "usd"

    def test_uses_description_fallback(self) -> None:
        instrument = {"description": "  Apple   Inc.  "}
        result = apply_all_cleansing(instrument)
        assert result["name"] == "Apple Inc."

    def test_uses_symbol_fallback(self) -> None:
        instrument = {"symbol": "  aapl  "}
        result = apply_all_cleansing(instrument)
        assert result["ticker"] == "AAPL"

    def test_handles_empty_input(self) -> None:
        result = apply_all_cleansing({})
        assert result["name"] is None
        assert result["ticker"] is None
        assert result["currency_code"] is None


# ── Similarity scoring tests ──────────────────────────────────────────


class TestNameSimilarity:
    """Tests for the name similarity scoring function."""

    def test_exact_match(self) -> None:
        assert _name_similarity("Apple Inc.", "Apple Inc.") == 1.0

    def test_partial_match(self) -> None:
        score = _name_similarity("Apple Inc.", "Apple")
        assert score > 0 and score < 1.0

    def test_no_match(self) -> None:
        assert _name_similarity("Apple Inc.", "Microsoft Corp") == 0.0

    def test_token_order_independent(self) -> None:
        a = _name_similarity("Apple Inc", "Inc Apple")
        assert a == 1.0

    def test_with_none(self) -> None:
        assert _name_similarity(None, "Apple") == 0.0
        assert _name_similarity("Apple", None) == 0.0

    def test_case_insensitive(self) -> None:
        assert _name_similarity("apple inc", "APPLE INC") == 1.0


# ── Service tests ─────────────────────────────────────────────────────


class TestIdentityResolutionService:
    """Tests for the IdentityResolutionService pipeline."""

    @pytest.fixture
    def mock_uow(self):
        uow = MagicMock()
        uow.securities = AsyncMock()
        uow.security_listings = AsyncMock()
        uow.unresolved_securities = AsyncMock()
        uow.resolution_audit_log = AsyncMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=None)
        uow.commit = AsyncMock()
        return uow

    @pytest.fixture
    def mock_resolver(self):
        resolver = MagicMock()
        resolver.resolve_by_isin = AsyncMock()
        resolver.resolve_by_figi = AsyncMock()
        resolver.resolve_by_ticker = AsyncMock()
        return resolver

    @pytest.fixture
    def mock_gateway(self):
        gateway = MagicMock()
        gateway.is_degraded = True  # Default to degraded (no OpenBB calls)
        gateway.get_historical_prices = AsyncMock()
        gateway.update_freshness = AsyncMock()
        return gateway

    @pytest.fixture
    def service(self, mock_uow, mock_resolver, mock_gateway):
        return IdentityResolutionService(
            uow=mock_uow,
            resolver=mock_resolver,
            gateway=mock_gateway,
        )

    # ── Stage 1: ISIN exact match ───────────────────────────────────

    async def test_stage1_exact_isin_resolves(self, service, mock_resolver):
        """Stage 1 resolves a security when ISIN matches locally."""
        mock_resolver.resolve_by_isin.return_value = ResolvedSecurity(
            security_id="sec_001",
            isin="US0378331005",
            ticker="AAPL",
            name="Apple Inc.",
            currency_code="USD",
            confidence="exact",
            source="local_db",
        )
        result = await service._stage_1_exact_isin({"isin": "US0378331005"})
        assert result is not None
        assert result.security_id == "sec_001"
        assert result.confidence == "exact"

    async def test_stage1_no_isin_returns_none(self, service):
        """Stage 1 returns None when no ISIN is provided."""
        result = await service._stage_1_exact_isin({"ticker": "AAPL"})
        assert result is None

    async def test_stage1_isin_not_found(
        self, service, mock_resolver, mock_uow
    ):
        """Stage 1 returns None when ISIN is not in local DB."""
        mock_resolver.resolve_by_isin.return_value = UnresolvedDTO(
            identifier="US0378331005",
            identifier_type="isin",
            reason="Not found",
        )
        mock_uow.security_listings.list.return_value = []
        result = await service._stage_1_exact_isin({"isin": "US0378331005"})
        assert result is None

    # ── Stage 2: FIGI / Ticker match ────────────────────────────────

    async def test_stage2_figi_resolves(self, service, mock_resolver):
        """Stage 2 resolves via FIGI lookup."""
        mock_resolver.resolve_by_figi.return_value = ResolvedSecurity(
            security_id="sec_002",
            figi="BBG000B9XRY4",
            ticker="AAPL",
            name="Apple Inc.",
            currency_code="USD",
            confidence="exact",
            source="openbb",
        )
        result = await service._stage_2_figi_ticker(
            {"figi": "BBG000B9XRY4", "ticker": "AAPL"}, "trading212"
        )
        assert result is not None
        assert result.security_id == "sec_002"

    async def test_stage2_ticker_resolves(
        self, service, mock_resolver, mock_uow
    ):
        """Stage 2 resolves via ticker when FIGI is absent."""
        mock_resolver.resolve_by_figi.return_value = UnresolvedDTO(
            identifier="BBG000B9XRY4",
            identifier_type="figi",
            reason="Not found",
        )
        mock_resolver.resolve_by_ticker.return_value = ResolvedSecurity(
            security_id="sec_003",
            ticker="AAPL",
            name="Apple Inc.",
            currency_code="USD",
            confidence="ticker_only",
            source="local_db",
        )
        result = await service._stage_2_figi_ticker(
            {"figi": "BBG000B9XRY4", "ticker": "AAPL"}, "trading212"
        )
        assert result is not None
        assert result.security_id == "sec_003"

    async def test_stage2_no_identifiers(self, service):
        """Stage 2 returns None when no FIGI or ticker."""
        result = await service._stage_2_figi_ticker(
            {"name": "Something"}, "test"
        )
        assert result is None

    # ── Stage 3: Fuzzy name match ───────────────────────────────────

    async def test_stage3_fuzzy_matches(self, service, mock_uow):
        """Stage 3 finds a matching security by name similarity."""
        from finance_sync.models.security import Security

        mock_sec = MagicMock(spec=Security)
        mock_sec.id = "sec_fuzzy_1"
        mock_sec.isin = "US0378331005"
        mock_sec.figi = None
        mock_sec.ticker = "AAPL"
        mock_sec.name = "Apple Inc."
        mock_sec.currency_code = "USD"

        mock_uow.securities.list.return_value = [mock_sec]

        result = await service._stage_3_fuzzy_name({"name": "Apple Inc"})
        assert result is not None
        assert result.security_id == "sec_fuzzy_1"
        assert result.confidence == "medium"

    async def test_stage3_no_match_below_threshold(self, service, mock_uow):
        """Stage 3 returns None when no name is close enough."""
        from finance_sync.models.security import Security

        mock_sec = MagicMock(spec=Security)
        mock_sec.id = "sec_other"
        mock_sec.isin = None
        mock_sec.figi = None
        mock_sec.ticker = "MSFT"
        mock_sec.name = "Microsoft Corporation"
        mock_sec.currency_code = "USD"

        mock_uow.securities.list.return_value = [mock_sec]

        result = await service._stage_3_fuzzy_name({"name": "Banana Republic"})
        assert result is None

    async def test_stage3_no_name(self, service):
        """Stage 3 returns None when no name is provided."""
        result = await service._stage_3_fuzzy_name({"ticker": "AAPL"})
        assert result is None

    async def test_stage3_empty_securities_list(self, service, mock_uow):
        """Stage 3 returns None when no canonical securities exist."""
        mock_uow.securities.list.return_value = []
        result = await service._stage_3_fuzzy_name({"name": "Apple Inc."})
        assert result is None

    # ── Stage 4: Manual queue ───────────────────────────────────────

    async def test_stage4_creates_unresolved_record(self, service, mock_uow):
        """Stage 4 stores an unresolved security."""
        mock_uow.unresolved_securities.list.return_value = []
        mock_uow.unresolved_securities.add = AsyncMock(
            return_value=MagicMock(id="unres_001")
        )

        result = await service._stage_4_enqueue(
            "trading212",
            {
                "ticker": "UNKNOWN",
                "name": "Unknown Corp",
                "isin": "US0000000000",
            },
        )
        assert result is not None
        assert mock_uow.unresolved_securities.add.called

    async def test_stage4_updates_existing(self, service, mock_uow):
        """Stage 4 updates existing unresolved record instead of duplicating."""
        existing = MagicMock(spec=UnresolvedSecurity)
        existing.external_security_id = "EXT001"
        existing.provider_key = "trading212"
        mock_uow.unresolved_securities.list.return_value = [existing]
        mock_uow.unresolved_securities.update = AsyncMock()

        result = await service._stage_4_enqueue(
            "trading212",
            {"id": "EXT001", "ticker": "SOME", "name": "Some Corp"},
        )
        assert result is not None
        assert mock_uow.unresolved_securities.update.called
        assert not mock_uow.unresolved_securities.add.called

    # ── Full pipeline ──────────────────────────────────────────────

    async def test_full_pipeline_resolves_all_isin(
        self, service, mock_resolver, mock_uow
    ):
        """Full pipeline resolves securities that match by ISIN."""
        mock_resolver.resolve_by_isin.return_value = ResolvedSecurity(
            security_id="sec_001",
            isin="US0378331005",
            ticker="AAPL",
            name="Apple Inc.",
            currency_code="USD",
            confidence="exact",
            source="local_db",
        )
        mock_uow.resolution_audit_log.add = AsyncMock()
        mock_uow.unresolved_securities.list.return_value = []
        mock_uow.unresolved_securities.add = AsyncMock()

        result = await service.process_incoming_securities(
            "test_provider",
            [{"isin": "US0378331005", "ticker": "AAPL", "name": "Apple Inc."}],
        )

        assert isinstance(result, ResolutionPipelineResult)
        assert result.total_input == 1
        assert result.resolved_auto == 1
        assert result.resolved_fuzzy == 0
        assert result.unresolved == 0
        assert result.audit_entries == 1
        assert mock_uow.resolution_audit_log.add.called

    async def test_full_pipeline_some_unresolved(
        self, service, mock_resolver, mock_uow
    ):
        """Full pipeline routes unresolvable securities to the manual queue."""
        mock_resolver.resolve_by_isin.return_value = UnresolvedDTO(
            identifier="UNKNOWN", identifier_type="isin", reason="Not found"
        )
        mock_resolver.resolve_by_figi.return_value = UnresolvedDTO(
            identifier="FIGI", identifier_type="figi", reason="Not found"
        )
        mock_resolver.resolve_by_ticker.return_value = UnresolvedDTO(
            identifier="TICK", identifier_type="ticker", reason="Not found"
        )

        mock_uow.securities.list.return_value = []
        mock_uow.security_listings.list.return_value = []  # no listings match
        mock_uow.resolution_audit_log.add = AsyncMock()
        mock_uow.unresolved_securities.list.return_value = []
        mock_uow.unresolved_securities.add = AsyncMock()

        result = await service.process_incoming_securities(
            "test_provider",
            [
                {
                    "isin": "UNKNOWN",
                    "ticker": "TICK",
                    "name": "Totally Unknown Corp",
                }
            ],
        )

        assert result.total_input == 1
        assert result.resolved_auto == 0
        assert result.unresolved == 1
        assert result.audit_entries == 0

    # ── Manual resolution ──────────────────────────────────────────

    async def test_manually_resolve_success(self, service, mock_uow):
        """Manually resolve unresolved to canonical and create audit."""
        unresolved = MagicMock(spec=UnresolvedSecurity)
        unresolved.id = "unres_001"
        unresolved.external_security_id = "EXT001"
        unresolved.raw_isin = None
        unresolved.raw_ticker = "UNKNOWN"
        unresolved.provider_key = "test"

        target = MagicMock()
        target.id = "sec_target_1"

        mock_uow.unresolved_securities.get = AsyncMock(return_value=unresolved)
        mock_uow.securities.get = AsyncMock(return_value=target)
        mock_uow.unresolved_securities.update = AsyncMock()
        mock_uow.resolution_audit_log.add = AsyncMock()

        result = await service.manually_resolve(
            unresolved_id="unres_001",
            target_security_id="sec_target_1",
            resolver_principal="test_user",
            resolution_notes="Test manual resolution",
        )

        assert result is not None
        assert result.target_security_id == "sec_target_1"
        assert result.resolution_method == "manual"
        assert mock_uow.unresolved_securities.update.called
        assert mock_uow.resolution_audit_log.add.called

    async def test_manually_resolve_nonexistent(self, service, mock_uow):
        """Manual resolution returns None for non-existent unresolved record."""
        mock_uow.unresolved_securities.get = AsyncMock(return_value=None)

        result = await service.manually_resolve(
            unresolved_id="nonexistent",
            target_security_id="sec_target_1",
        )
        assert result is None

    async def test_manually_resolve_nonexistent_target(self, service, mock_uow):
        """Manual resolution returns None for non-existent target security."""
        mock_uow.unresolved_securities.get = AsyncMock(
            return_value=MagicMock(spec=UnresolvedSecurity)
        )
        mock_uow.securities.get = AsyncMock(return_value=None)

        result = await service.manually_resolve(
            unresolved_id="unres_001",
            target_security_id="nonexistent_target",
        )
        assert result is None

    # ── Map and resolve ────────────────────────────────────────────

    async def test_map_and_resolve_creates_if_not_exists(
        self, service, mock_uow
    ):
        """Map creates an unresolved record if none exists."""
        mock_uow.unresolved_securities.list.return_value = []
        mock_uow.unresolved_securities.add = AsyncMock(
            return_value=MagicMock(id="new_unres")
        )
        mock_uow.securities.get = AsyncMock(
            return_value=MagicMock(id="sec_target")
        )
        mock_uow.unresolved_securities.update = AsyncMock()
        mock_uow.resolution_audit_log.add = AsyncMock()

        result = await service.map_and_resolve(
            provider_key="test",
            external_security_id="EXT001",
            target_security_id="sec_target",
        )
        assert result is not None
        assert result.target_security_id == "sec_target"

    async def test_map_and_resolve_uses_existing(self, service, mock_uow):
        """Map uses existing unresolved record if found."""
        existing = MagicMock(spec=UnresolvedSecurity)
        existing.id = "existing_unres"
        mock_uow.unresolved_securities.list.return_value = [existing]
        mock_uow.securities.get = AsyncMock(
            return_value=MagicMock(id="sec_target")
        )
        mock_uow.unresolved_securities.update = AsyncMock()
        mock_uow.resolution_audit_log.add = AsyncMock()

        result = await service.map_and_resolve(
            provider_key="test",
            external_security_id="EXT001",
            target_security_id="sec_target",
        )
        assert result is not None
        assert not mock_uow.unresolved_securities.add.called

    # ── Get unresolved / audit log ─────────────────────────────────

    async def test_get_unresolved_filters_unmapped(self, service, mock_uow):
        """get_unresolved returns only unmapped records."""
        mock_uow.unresolved_securities.list.return_value = []

        result = await service.get_unresolved(only_unmapped=True)
        assert result is not None
        assert mock_uow.unresolved_securities.list.called

    async def test_get_unresolved_filters_by_provider(self, service, mock_uow):
        """get_unresolved filters by provider_key."""
        mock_uow.unresolved_securities.list.return_value = []

        result = await service.get_unresolved(provider_key="trading212")
        assert result is not None

    async def test_get_audit_log_lists_entries(self, service, mock_uow):
        """get_audit_log returns audit entries."""
        mock_uow.resolution_audit_log.list.return_value = []

        result = await service.get_audit_log()
        assert result is not None

    # ── Edge cases ────────────────────────────────────────────────

    async def test_pipeline_handles_empty_input(self, service):
        """Pipeline handles empty input gracefully."""
        result = await service.process_incoming_securities("test", [])
        assert result.total_input == 0
        assert result.resolved_auto == 0
        assert result.unresolved == 0

    async def test_pipeline_handles_multiple_instruments(
        self, service, mock_resolver, mock_uow
    ):
        """Pipeline processes multiple instruments correctly."""
        mock_resolver.resolve_by_isin.side_effect = [
            ResolvedSecurity(
                security_id="sec_001",
                isin="US0378331005",
                ticker="AAPL",
                name="Apple Inc.",
                currency_code="USD",
                confidence="exact",
                source="local_db",
            ),
            UnresolvedDTO(
                identifier="NONE", identifier_type="isin", reason="Not found"
            ),
        ]
        mock_resolver.resolve_by_figi.return_value = UnresolvedDTO(
            identifier="FIGI", identifier_type="figi", reason="Not found"
        )
        mock_resolver.resolve_by_ticker.return_value = UnresolvedDTO(
            identifier="TICK", identifier_type="ticker", reason="Not found"
        )
        mock_uow.securities.list.return_value = []
        mock_uow.security_listings.list.return_value = []
        mock_uow.resolution_audit_log.add = AsyncMock()
        mock_uow.unresolved_securities.list.return_value = []
        mock_uow.unresolved_securities.add = AsyncMock()

        result = await service.process_incoming_securities(
            "test",
            [
                {"isin": "US0378331005", "name": "Apple Inc."},
                {"isin": "NONE", "ticker": "UNKN", "name": "Unknown Corp"},
            ],
        )

        assert result.total_input == 2
        assert result.resolved_auto == 1
        assert result.unresolved == 1

    async def test_stage1_isin_case_normalised(self, service, mock_resolver):
        """Stage 1 normalises ISIN to uppercase."""
        mock_resolver.resolve_by_isin.return_value = ResolvedSecurity(
            security_id="sec_001",
            isin="US0378331005",
            name="Apple Inc.",
            currency_code="USD",
            confidence="exact",
            source="local_db",
        )
        result = await service._stage_1_exact_isin({"isin": "us0378331005"})
        assert result is not None
        mock_resolver.resolve_by_isin.assert_called_once_with("US0378331005")
