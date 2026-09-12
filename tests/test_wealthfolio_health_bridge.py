"""Contract tests for Wealthfolio health issue normalization."""

import pytest

from finance_sync.services.wealthfolio_health_bridge import (
    health_payload_hash,
    normalize_health_issues,
)
from finance_sync.reconciliation.remediation.backlog import deduplication_key


def test_normalization_is_one_traceable_item_per_affected_asset() -> None:
    issues = normalize_health_issues(
        {
            "issues": [
                {
                    "code": "MISSING_HISTORICAL_PRICES",
                    "severity": "error",
                    "affectedItems": [{"id": "asset-1"}, {"id": "asset-2"}],
                    "details": "bounded detail",
                }
            ]
        },
        tenant_id="tenant-1",
        target_id="target-1",
    )
    assert len(issues) == 2
    assert {issue.affected_entity_id for issue in issues} == {"asset-1", "asset-2"}
    assert all(issue.issue_type == "wealthfolio_historical_price_gap" for issue in issues)
    assert all(issue.context["target_id"] == "target-1" for issue in issues)


def test_target_identity_is_part_of_backlog_identity() -> None:
    payload = {
        "issues": [
            {
                "code": "MISSING_HISTORICAL_PRICES",
                "affectedItems": [{"id": "asset-1"}],
            }
        ]
    }
    target_a = normalize_health_issues(
        payload, tenant_id="tenant-1", target_id="target-a"
    )[0]
    target_b = normalize_health_issues(
        payload, tenant_id="tenant-1", target_id="target-b"
    )[0]

    assert target_a.connection_id == "target-a"
    assert target_a.scope == "target-a"
    assert target_b.connection_id == "target-b"
    assert target_a.context["target_id"] == "target-a"
    assert target_b.context["target_id"] == "target-b"
    assert deduplication_key(target_a) != deduplication_key(target_b)


def test_unknown_category_fails_closed_to_manual_review_strategy() -> None:
    issues = normalize_health_issues(
        {"issues": [{"category": "unexpected", "affectedItems": ["asset"]}]},
        tenant_id="tenant-1",
        target_id="target-1",
    )
    assert issues[0].issue_type == "wealthfolio_unsupported_issue"
    assert issues[0].remediation_strategy == "unsupported"
    assert issues[0].context["manual_review"] is True


@pytest.mark.parametrize(
    ("code", "expected_kind"),
    [
        ("MISSING_PURCHASE_PRICE", "wealthfolio_missing_purchase_price"),
        ("NEGATIVE_VALUATION", "wealthfolio_negative_valuation"),
        ("INCOMPLETE_VALUATION", "wealthfolio_incomplete_valuation"),
        ("TRANSACTION_MISMATCH", "wealthfolio_transaction_or_transfer_issue"),
        ("TRANSFER_MISMATCH", "wealthfolio_transaction_or_transfer_issue"),
        ("FUTURE_UNKNOWN_HEALTH_CODE", "wealthfolio_unsupported_issue"),
    ],
)
def test_unsafe_categories_precede_generic_price_and_are_manual_only(
    code: str, expected_kind: str
) -> None:
    finding = normalize_health_issues(
        {
            "issues": [
                {
                    "code": code,
                    "affectedItems": [{"id": "asset-1"}],
                }
            ]
        },
        tenant_id="tenant-1",
        target_id="target-1",
    )[0]

    assert finding.issue_type == expected_kind
    assert finding.remediation_strategy == "unsupported"
    assert finding.context["manual_review"] is True


def test_identifier_type_is_preserved_for_isin_and_ticker() -> None:
    findings = normalize_health_issues(
        {
            "issues": [
                {
                    "code": "MISSING_QUOTE",
                    "affectedItems": [
                        {"id": "isin-asset", "isin": "US123"},
                        {"id": "ticker-asset", "ticker": "ABC"},
                    ],
                }
            ]
        },
        tenant_id="tenant-1",
        target_id="target-1",
    )

    assert findings[0].context["identifier_type"] == "isin"
    assert findings[1].context["identifier_type"] == "ticker"


def test_payload_hash_is_order_independent() -> None:
    assert health_payload_hash({"issues": [], "status": "ok"}) == health_payload_hash(
        {"status": "ok", "issues": []}
    )
