"""Contract tests for Wealthfolio health issue normalization."""

from types import SimpleNamespace

import pytest

from finance_sync.models.wealthfolio_health_cursor import (
    WealthfolioHealthCursor,
)
from finance_sync.reconciliation.remediation.backlog import deduplication_key
from finance_sync.services.wealthfolio_health_bridge import (
    WealthfolioHealthBridge,
    health_payload_hash,
    normalize_health_issues,
    prepare_health_poll,
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
    assert {issue.affected_entity_id for issue in issues} == {
        "asset-1",
        "asset-2",
    }
    assert all(
        issue.issue_type == "wealthfolio_historical_price_gap"
        for issue in issues
    )
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

    assert target_a.target_id == "target-a"
    assert target_a.scope == "target-a"
    assert target_b.target_id == "target-b"
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
    assert health_payload_hash(
        {"issues": [], "status": "ok"}
    ) == health_payload_hash({"status": "ok", "issues": []})


def test_over_limit_health_snapshot_is_bounded_but_incomplete() -> None:
    result = prepare_health_poll(
        {
            "issues": [
                {"code": "MISSING_PRICE", "affectedItems": [str(i)]}
                for i in range(3)
            ]
        },
        issue_limit=2,
    )

    assert result.complete is False
    assert result.truncated is True
    assert len(result.payload["issues"]) == 2
    assert result.cursor_state == {
        "issue_limit": 2,
        "returned_issues": 2,
        "reason": "issue_limit",
    }


def test_complete_empty_health_snapshot_is_reconcilable() -> None:
    result = prepare_health_poll({"issues": []}, issue_limit=2)

    assert result.complete is True
    assert result.truncated is False
    assert result.cursor_state["reason"] == "complete"


def test_malformed_health_snapshot_is_incomplete() -> None:
    result = prepare_health_poll({"status": "ok"}, issue_limit=2)

    assert result.complete is False
    assert result.truncated is False
    assert result.cursor_state["reason"] == "malformed_issues"


@pytest.mark.asyncio
async def test_incomplete_success_persists_cursor_and_does_not_reconcile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, value: object) -> None:
            self.value = value

        def scalar_one_or_none(self) -> object:
            return self.value

        def scalars(self) -> list[object]:
            return active_items

    class Session:
        def __init__(self) -> None:
            self.responses = [Result(cursor), Result(None)]

        async def execute(self, _statement: object) -> Result:
            return self.responses.pop(0)

        async def flush(self) -> None:
            return None

    transitions: list[str] = []

    class Backlog:
        def __init__(self, _session: object) -> None:
            pass

        async def register(self, _issue: object) -> object:
            return SimpleNamespace()

        async def transition(
            self, _tenant: str, item_id: str, **_kwargs: object
        ) -> None:
            transitions.append(item_id)

    active_items = [
        SimpleNamespace(
            id="active-1",
            deduplication_key="not-current",
            issue_type="wealthfolio_quote_sync_failure",
            status="pending",
        )
    ]
    cursor = WealthfolioHealthCursor(tenant_id="tenant-1", target_id="target-1")
    monkeypatch.setattr(
        "finance_sync.services.wealthfolio_health_bridge.BacklogRepository",
        Backlog,
    )

    bridge = WealthfolioHealthBridge(
        Session(), "tenant-1", SimpleNamespace(id="target-1")
    )
    await bridge.enqueue_success(
        {"issues": [{"code": "MISSING_PRICE", "affectedItems": ["asset"]}]},
        complete=False,
        truncated=True,
        cursor_state={"reason": "issue_limit", "issue_limit": 1},
    )

    assert cursor.complete is False
    assert cursor.truncated is True
    assert cursor.cursor_state["reason"] == "issue_limit"
    assert transitions == []


@pytest.mark.asyncio
async def test_failed_poll_persists_error_without_marking_cursor_complete() -> (
    None
):
    class Result:
        def scalar_one_or_none(self) -> object:
            return cursor

    class Session:
        async def execute(self, _statement: object) -> Result:
            return Result()

        async def flush(self) -> None:
            return None

    cursor = WealthfolioHealthCursor(
        tenant_id="tenant-1",
        target_id="target-1",
        complete=True,
        truncated=False,
    )
    bridge = WealthfolioHealthBridge(
        Session(), "tenant-1", SimpleNamespace(id="target-1")
    )

    await bridge.record_failure(category="transport", message="poll failed")

    assert cursor.complete is False
    assert cursor.truncated is False
    assert cursor.cursor_state == {"reason": "poll_failed"}
    assert cursor.last_error_category == "transport"


def test_cursor_model_and_migration_expose_durable_completeness_state() -> None:
    columns = WealthfolioHealthCursor.__table__.c
    assert columns.complete.nullable is False
    assert columns.truncated.nullable is False
    assert columns.cursor_state.nullable is False
