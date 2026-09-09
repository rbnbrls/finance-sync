"""Tests for ``finance_sync.sync.errors`` — safe ``since`` validation.

Regression coverage for the trading212 connection sync path: ``since``
values that are missing, truncated, or malformed must be normalised (or
rejected with a reason that never echoes the raw input) before they reach
a provider-specific connector.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from finance_sync.connectors.exceptions import (
    PermanentError,
    RateLimitError,
    TransientError,
)
from finance_sync.sync.errors import (
    InvalidSinceError,
    categorize_sync_error,
    classify_sync_error,
    safe_sync_error_message,
    validate_since,
)

DEFAULT = datetime(2026, 1, 1, tzinfo=UTC)


# ── validate_since ───────────────────────────────────────────────────


class TestValidateSince:
    def test_none_returns_default(self) -> None:
        assert validate_since(None, default=DEFAULT) == DEFAULT

    def test_empty_string_returns_default(self) -> None:
        assert validate_since("", default=DEFAULT) == DEFAULT
        assert validate_since("   ", default=DEFAULT) == DEFAULT

    def test_full_iso_string_with_offset(self) -> None:
        got = validate_since("2026-05-29T13:04:07.465267+00:00")
        assert got == datetime(2026, 5, 29, 13, 4, 7, 465267, tzinfo=UTC)

    def test_truncated_microseconds_no_tz_is_utc(self) -> None:
        got = validate_since("2026-05-29T13:04:07.465")
        assert got == datetime(2026, 5, 29, 13, 4, 7, 465000, tzinfo=UTC)
        assert got.tzinfo is not None

    def test_truncated_seconds_no_tz_is_utc(self) -> None:
        got = validate_since("2026-05-29T13:04:07")
        assert got == datetime(2026, 5, 29, 13, 4, 7, tzinfo=UTC)

    def test_date_only_is_utc_midnight(self) -> None:
        got = validate_since("2026-05-29")
        assert got == datetime(2026, 5, 29, tzinfo=UTC)

    def test_z_suffix_supported(self) -> None:
        got = validate_since("2026-05-29T13:04:07Z")
        assert got == datetime(2026, 5, 29, 13, 4, 7, tzinfo=UTC)

    def test_offset_is_normalised_to_utc(self) -> None:
        got = validate_since("2026-05-29T15:04:07+02:00")
        assert got == datetime(2026, 5, 29, 13, 4, 7, tzinfo=UTC)

    def test_naive_datetime_is_utc(self) -> None:
        got = validate_since(datetime(2026, 5, 29, 13, 4, 7))
        assert got == datetime(2026, 5, 29, 13, 4, 7, tzinfo=UTC)

    def test_aware_datetime_keeps_instant(self) -> None:
        aware = datetime(2026, 5, 29, 15, 4, 7, tzinfo=UTC) - timedelta(hours=2)
        got = validate_since(aware)
        assert got == datetime(2026, 5, 29, 13, 4, 7, tzinfo=UTC)

    @pytest.mark.parametrize(
        "bad",
        [
            "garbage",
            "2026-13-45",
            "T13:04:07",
            "2026-05-29T13:04:07.465267+00:00 extra",
            "not-a-date",
        ],
    )
    def test_unparseable_string_raises_without_echoing_value(
        self, bad: str
    ) -> None:
        with pytest.raises(InvalidSinceError) as exc_info:
            validate_since(bad, default=DEFAULT)
        # The raw value must never appear in the error message.  (The
        # static example ``2026-05-29T13:04:07Z`` in the message is fine —
        # it is a fixed format illustration, not caller input.)
        if bad not in ("T13:04:07", "2026-05-29T13:04:07.465267+00:00 extra"):
            assert bad not in str(exc_info.value)

    @pytest.mark.parametrize("bad", [12345, 1.5, ["x"], {"x": 1}, b"bytes"])
    def test_wrong_type_raises_without_echoing_value(self, bad: object) -> None:
        with pytest.raises(InvalidSinceError) as exc_info:
            validate_since(bad, default=DEFAULT)
        assert str(bad) not in str(exc_info.value)


# ── categorise_sync_error ───────────────────────────────────────────


class TestCategorizeInvalidSince:
    def test_invalid_since_maps_to_validation(self) -> None:
        err = InvalidSinceError("not a valid ISO-8601 datetime")
        assert categorize_sync_error(err) == "validation"
        assert classify_sync_error(err).value == "permanent"

    def test_other_error_categories_unchanged(self) -> None:
        assert categorize_sync_error(RateLimitError("x")) == "rate_limited"
        assert (
            categorize_sync_error(TransientError("x")) == "provider_unavailable"
        )
        assert (
            categorize_sync_error(PermanentError("invalid 400")) == "validation"
        )
        assert categorize_sync_error(PermanentError("auth failed")) in (
            "reauth_required",
            "authentication",
        )

    def test_database_failure_has_actionable_safe_message(self) -> None:
        error = IntegrityError("insert", {}, ValueError("duplicate"))
        assert categorize_sync_error(error) == "database"
        assert safe_sync_error_message(error) == "Database error while syncing"
