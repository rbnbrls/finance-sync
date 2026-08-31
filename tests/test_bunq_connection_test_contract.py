"""Regression tests for Bunq connection-test validation."""

from finance_sync.api.v1.connectors_config import (
    _account_enumeration_error_is_fatal,
)


def test_bunq_account_enumeration_failure_is_fatal() -> None:
    """A Bunq session without accounts cannot support a sync."""
    assert _account_enumeration_error_is_fatal("bunq") is True


def test_other_connectors_keep_best_effort_account_enumeration() -> None:
    """Providers with optional scopes keep the existing compatibility path."""
    assert _account_enumeration_error_is_fatal("trading212") is False
