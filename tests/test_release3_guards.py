"""Regression tests for Release 3 boundaries and safe error handling."""

from sqlalchemy import literal_column

from finance_sync.connectors.exceptions import PermanentError, TransientError
from finance_sync.services.read.pagination import expression, sort_field
from finance_sync.sync.errors import (
    SyncErrorKind,
    classify_sync_error,
    safe_sync_error_message,
)


def test_sync_errors_have_stable_categories_and_safe_internal_message() -> None:
    assert (
        classify_sync_error(TransientError("timeout"))
        is SyncErrorKind.TRANSIENT
    )
    assert classify_sync_error(PermanentError("invalid credentials")) is (
        SyncErrorKind.PERMANENT
    )
    assert classify_sync_error(RuntimeError("secret=/tmp/private")) is (
        SyncErrorKind.INTERNAL
    )
    assert safe_sync_error_message(RuntimeError("secret=/tmp/private")) == (
        "Sync failed due to an internal error"
    )
    assert safe_sync_error_message(PermanentError("invalid credentials")) == (
        "invalid credentials"
    )


def test_read_pagination_helpers_default_unknown_sort_fields() -> None:
    mapping = {
        "name": literal_column("name"),
        "created_at": literal_column("created_at"),
    }
    assert sort_field(mapping, "unknown", "asc") is not None
    assert expression() is True
