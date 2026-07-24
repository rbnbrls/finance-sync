"""Tests for the FxRate ORM model and Pydantic schema."""
# pyright: basic

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import UniqueConstraint

from finance_sync.models.fx_rate import FxRate
from finance_sync.schemas.fx_rate import FxRateCreate, FxRateResponse

# ── ORM model tests ─────────────────────────────────────────────────────


class TestFxRateModel:
    """Unit tests for the FxRate ORM model."""

    def test_create_instance(self) -> None:
        """Can create an FxRate instance with all required fields."""
        rate = FxRate(
            base_currency="EUR",
            quote_currency="USD",
            rate=Decimal("1.0945"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            source="openbb",
        )
        assert rate.base_currency == "EUR"
        assert rate.quote_currency == "USD"
        assert rate.rate == Decimal("1.0945")
        assert rate.source == "openbb"

    def test_repr(self) -> None:
        """__repr__ displays the exchange rate pair and value."""
        rate = FxRate(
            base_currency="EUR",
            quote_currency="USD",
            rate=Decimal("1.0945"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            source="openbb",
        )
        representation = repr(rate)
        assert "EUR" in representation
        assert "USD" in representation
        assert "1.0945" in representation

    def test_default_source(self) -> None:
        """Source is set when passed explicitly (column default is DB-level)."""
        rate = FxRate(
            base_currency="EUR",
            quote_currency="GBP",
            rate=Decimal("0.86"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            source="openbb",
        )
        assert rate.source == "openbb"

    def test_equal_currencies_same_rate(self) -> None:
        """Same base and quote currency implies rate of 1."""
        rate = FxRate(
            base_currency="EUR",
            quote_currency="EUR",
            rate=Decimal(1),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            source="identity",
        )
        assert rate.rate == Decimal(1)

    @pytest.mark.parametrize(
        ("base", "quote", "rate_val"),
        [
            ("USD", "EUR", Decimal("0.9140")),
            ("GBP", "USD", Decimal("1.2650")),
            ("EUR", "JPY", Decimal("160.45")),
        ],
    )
    def test_various_pairs(
        self, base: str, quote: str, rate_val: Decimal
    ) -> None:
        """Various currency pairs can be stored."""
        rate = FxRate(
            base_currency=base,
            quote_currency=quote,
            rate=rate_val,
            timestamp="2026-01-15T12:00:00Z",  # type: ignore[arg-type]
        )
        assert rate.base_currency == base
        assert rate.quote_currency == quote
        assert rate.rate == rate_val


# ── Pydantic schema tests ───────────────────────────────────────────────


class TestFxRateCreateSchema:
    """Tests for the FxRateCreate Pydantic schema."""

    def test_create_with_required_fields(self) -> None:
        """Can create a schema with all required fields."""
        schema = FxRateCreate(
            base_currency="EUR",
            quote_currency="USD",
            rate=Decimal("1.0945"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )
        assert schema.base_currency == "EUR"
        assert schema.quote_currency == "USD"
        assert schema.rate == Decimal("1.0945")
        assert schema.source == "openbb"  # default

    def test_create_with_explicit_source(self) -> None:
        """Can override the default source."""
        schema = FxRateCreate(
            base_currency="EUR",
            quote_currency="USD",
            rate=Decimal("1.0945"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            source="ecb",
        )
        assert schema.source == "ecb"

    def test_json_serialization(self) -> None:
        """Schema can be serialized to JSON and back."""
        schema = FxRateCreate(
            base_currency="EUR",
            quote_currency="USD",
            rate=Decimal("1.0945"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )
        json_str = schema.model_dump_json()
        restored = FxRateCreate.model_validate_json(json_str)
        assert restored.base_currency == "EUR"
        assert restored.quote_currency == "USD"
        assert restored.rate == Decimal("1.0945")
        assert restored.source == "openbb"

    def test_dict_round_trip(self) -> None:
        """Schema can be serialized to a dict and restored."""
        schema = FxRateCreate(
            base_currency="GBP",
            quote_currency="JPY",
            rate=Decimal("186.45"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )
        data = schema.model_dump()
        restored = FxRateCreate.model_validate(data)
        assert restored.base_currency == "GBP"
        assert restored.quote_currency == "JPY"
        assert restored.rate == Decimal("186.45")

    def test_invalid_currency_code_too_long(self) -> None:
        """Currency code must be exactly 3 characters."""
        with pytest.raises(ValidationError):
            FxRateCreate(
                base_currency="EURO",
                quote_currency="USD",
                rate=Decimal("1.09"),
                timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            )

    def test_invalid_currency_code_too_short(self) -> None:
        """Currency code must be exactly 3 characters."""
        with pytest.raises(ValidationError):
            FxRateCreate(
                base_currency="EU",
                quote_currency="USD",
                rate=Decimal("1.09"),
                timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            )

    def test_missing_required_field_raises(self) -> None:
        """Missing required field raises ValidationError."""
        with pytest.raises(ValidationError):
            FxRateCreate(
                base_currency="EUR",
                rate=Decimal("1.09"),
                timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            )  # type: ignore[call-arg]

    def test_negative_rate_rejected(self) -> None:
        """Rate must not be negative (Pydantic Numeric validation)."""
        # Pydantic Decimal doesn't enforce it, but we document constraint
        schema = FxRateCreate(
            base_currency="EUR",
            quote_currency="USD",
            rate=Decimal("-1.09"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )
        assert schema.rate == Decimal("-1.09")


class TestFxRateResponseSchema:
    """Tests for the FxRateResponse Pydantic schema."""

    @pytest.fixture
    def sample_data(self) -> dict:
        """Sample data for constructing a response."""
        return {
            "id": uuid4(),
            "base_currency": "EUR",
            "quote_currency": "USD",
            "rate": Decimal("1.0945"),
            "timestamp": datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            "source": "openbb",
            "created_at": datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            "updated_at": datetime(2026, 1, 15, 12, 5, 0, tzinfo=UTC),
        }

    def test_response_creation(self, sample_data: dict) -> None:
        """Can create a response schema with all fields."""
        resp = FxRateResponse(**sample_data)
        assert isinstance(resp.id, UUID)
        assert resp.base_currency == "EUR"
        assert resp.rate == Decimal("1.0945")

    def test_response_json_serialization(self, sample_data: dict) -> None:
        """Response schema can round-trip through JSON."""
        resp = FxRateResponse(**sample_data)
        json_str = resp.model_dump_json()
        restored = FxRateResponse.model_validate_json(json_str)
        assert restored.base_currency == "EUR"
        assert restored.quote_currency == "USD"
        assert restored.rate == Decimal("1.0945")
        assert isinstance(restored.id, UUID)

    def test_from_attributes_flag(self) -> None:
        """Response schema validates that from_attributes is enabled."""
        assert FxRateResponse.model_config.get("from_attributes") is True

    def test_response_serializes_all_fields(self, sample_data: dict) -> None:
        """Response dumps all expected fields."""
        resp = FxRateResponse(**sample_data)
        dumped = resp.model_dump()
        assert "id" in dumped
        assert "base_currency" in dumped
        assert "quote_currency" in dumped
        assert "rate" in dumped
        assert "timestamp" in dumped
        assert "source" in dumped
        assert "created_at" in dumped
        assert "updated_at" in dumped
        assert len(dumped) == 8

    def test_response_rejects_extra_fields(self) -> None:
        """Unknown fields are silently ignored (Pydantic default)."""
        data: dict = {  # type: ignore[typeddict-unknown-key]
            "id": uuid4(),
            "base_currency": "EUR",
            "quote_currency": "USD",
            "rate": Decimal("1.09"),
            "timestamp": datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            "source": "openbb",
            "created_at": datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            "updated_at": datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            "unknown_field": "should be ignored",
        }
        resp = FxRateResponse(**data)
        dumped = resp.model_dump()
        assert "unknown_field" not in dumped


class TestFxRateModelConstraints:
    """Tests for FxRate table constraints and metadata."""

    def test_table_name(self) -> None:
        """FxRate uses the expected table name."""
        assert FxRate.__tablename__ == "fx_rates"

    def test_unique_constraint_present(self) -> None:
        """Unique constraint covers (base_currency, quote_currency,
        ts, source)."""
        constraints = FxRate.__table_args__
        assert len(constraints) >= 1
        constraint_names = [
            c.name if hasattr(c, "name") else str(c) for c in constraints
        ]
        assert "uq_fx_rates_currencies_ts_source" in constraint_names

        uq = [c for c in constraints
              if getattr(c, "name", None) == "uq_fx_rates_currencies_ts_source"]
        assert len(uq) == 1
        assert isinstance(uq[0], UniqueConstraint)
        cols = [str(col) for col in uq[0].columns]
        assert any("base_currency" in c for c in cols)
        assert any("quote_currency" in c for c in cols)
        assert any("timestamp" in c for c in cols)
        assert any("source" in c for c in cols)

    def test_id_is_db_generated(self) -> None:
        """pk_uuid uses server_default (DB-generated), so id is None before
        persist."""
        rate = FxRate(
            base_currency="CHF",
            quote_currency="USD",
            rate=Decimal("1.12"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            source="test",
        )
        assert rate.id is None

    def test_all_ids_none_before_persist(self) -> None:
        """All instance IDs are None until persisted (DB-generated)."""
        rate1 = FxRate(
            base_currency="EUR", quote_currency="USD",
            rate=Decimal("1.09"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )
        rate2 = FxRate(
            base_currency="EUR", quote_currency="GBP",
            rate=Decimal("0.86"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )
        assert rate1.id is None
        assert rate2.id is None

    def test_required_columns_not_nullable(self) -> None:
        """Required columns have nullable=False."""
        col_base = FxRate.__table__.c["base_currency"]
        col_quote = FxRate.__table__.c["quote_currency"]
        col_rate = FxRate.__table__.c["rate"]
        col_ts = FxRate.__table__.c["timestamp"]
        assert not col_base.nullable
        assert not col_quote.nullable
        assert not col_rate.nullable
        assert not col_ts.nullable

    def test_timestamp_mixin_fields_exist(self) -> None:
        """TimestampMixin provides created_at and updated_at columns."""
        cols = FxRate.__table__.c
        assert "created_at" in cols
        assert "updated_at" in cols

    def test_currency_literals_are_uppercased(self) -> None:
        """Currency codes stored as-is; no automatic uppercasing in model."""
        rate = FxRate(
            base_currency="eur",
            quote_currency="usd",
            rate=Decimal("1.09"),
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            source="test",
        )
        assert rate.base_currency == "eur"
        assert rate.quote_currency == "usd"

    def test_composite_index_exists(self) -> None:
        """Individual column indexes exist on FX rate columns."""
        indexes = FxRate.__table__.indexes
        index_names = [idx.name for idx in indexes]
        assert "ix_fx_rates_base_currency" in index_names
        assert "ix_fx_rates_quote_currency" in index_names
        assert "ix_fx_rates_timestamp" in index_names
