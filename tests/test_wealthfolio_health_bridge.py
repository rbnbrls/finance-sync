"""Contract tests for Wealthfolio health issue normalization."""

from finance_sync.services.wealthfolio_health_bridge import (
    health_payload_hash,
    normalize_health_issues,
)


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


def test_unknown_category_fails_closed_to_manual_review_strategy() -> None:
    issues = normalize_health_issues(
        {"issues": [{"category": "unexpected", "affectedItems": ["asset"]}]},
        tenant_id="tenant-1",
        target_id="target-1",
    )
    assert issues[0].issue_type == "wealthfolio_unsupported_issue"
    assert issues[0].remediation_strategy == "unsupported"
    assert issues[0].context["manual_review"] is True


def test_payload_hash_is_order_independent() -> None:
    assert health_payload_hash({"issues": [], "status": "ok"}) == health_payload_hash(
        {"status": "ok", "issues": []}
    )
