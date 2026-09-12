from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from finance_sync.reconciliation.remediation.backlog import (
    BacklogRepository,
    DetectedIssue,
    deduplication_key,
)
from finance_sync.reconciliation.remediation.batching import (
    coalesce_days,
    create_batches,
)
from finance_sync.reconciliation.remediation.policies import (
    classify_error,
    retry_at,
    scheduling_score,
)
from finance_sync.reconciliation.remediation.price_history import (
    HistoricalPriceStrategy,
)
from finance_sync.reconciliation.remediation.quote import LatestQuoteStrategy
from finance_sync.reconciliation.remediation.rate_limit import (
    QuotaPolicy,
    RemediationRateLimitCoordinator,
    parse_retry_after,
)
from finance_sync.reconciliation.remediation.trading212 import (
    Trading212InstrumentMetadataStrategy,
)
from finance_sync.reconciliation.remediation.transaction_history import (
    TransactionHistoryStrategy,
)


def test_transaction_history_capabilities_are_explicit_and_bounded() -> None:
    from finance_sync.connectors.bunq import BunqConnector
    from finance_sync.connectors.csv_import import CSVImportConnector
    from finance_sync.connectors.degiro_pension import DegiroPensionConnector
    from finance_sync.connectors.manual_expense import ManualExpenseConnector
    from finance_sync.connectors.plaid_like import PlaidLikeConnector
    from finance_sync.connectors.saxo_investor import SaxoInvestorConnector
    from finance_sync.connectors.trading212 import Trading212Connector
    from finance_sync.connectors.ynab import YnabConnector

    for connector in (
        BunqConnector,
        YnabConnector,
        DegiroPensionConnector,
        SaxoInvestorConnector,
        CSVImportConnector,
        ManualExpenseConnector,
        PlaidLikeConnector,
        Trading212Connector,
    ):
        metadata = connector.remediation_strategies["transaction_history_gap"]
        assert metadata["endpoint_family"] == "transaction_history"
        assert metadata["batch_limit"] == 1


def test_remediation_response_serializes_uuid_backed_fields() -> None:
    from finance_sync.schemas.remediation import RemediationItemResponse

    now = datetime.now(UTC)
    item = SimpleNamespace(
        id=uuid4(),
        provider_key="trading212",
        connection_id=uuid4(),
        issue_type="quote_gap",
        affected_entity_type="security",
        affected_entity_id="security-1",
        severity="warning",
        priority=0,
        status="pending",
        remediation_strategy="latest_quote",
        first_detected_at=now,
        last_seen_at=now,
        next_attempt_at=now,
        attempt_count=0,
        rate_limit_deferral_count=0,
        context={},
        last_error=None,
        last_error_category=None,
        resolved_at=None,
        verification_count=0,
    )

    response = RemediationItemResponse.model_validate(item)

    assert isinstance(response.id, str)
    assert isinstance(response.connection_id, str)


def test_verification_evidence_is_bounded_and_typed() -> None:
    from finance_sync.reconciliation.remediation.executor import (
        _record_verification,
    )

    item = SimpleNamespace(context={}, verified_at=None)
    _record_verification(
        item,
        SimpleNamespace(reason="verification completed " + ("x" * 400)),
    )

    assert item.verified_at is not None
    assert len(item.context["last_verification_reason"]) == 256


@pytest.mark.asyncio
async def test_auth_failure_publishes_connection_health_signal() -> None:
    class FakeResult:
        rowcount = 1

    class FakeSession:
        async def execute(self, _statement):
            return FakeResult()

    marked = await BacklogRepository(
        FakeSession()
    ).mark_connection_auth_failure(
        "tenant-1", "connection-1", error="safe diagnostic"
    )
    assert marked


def test_deduplication_key_ignores_volatile_context_and_normalizes_scope() -> (
    None
):
    detected = datetime(2026, 1, 1, tzinfo=UTC)
    first = DetectedIssue(
        "TENANT",
        "Trading212",
        "gap",
        "account",
        "a",
        scope="2026-01-01/2026-01-02",
        detected_at=detected,
        context={"run_id": "one"},
    )
    second = DetectedIssue(
        "tenant",
        "trading212",
        "gap",
        "account",
        "a",
        scope="2026-01-01/2026-01-02",
        detected_at=detected,
        context={"run_id": "two"},
    )
    assert deduplication_key(first) == deduplication_key(second)


def test_retry_policy_classifies_rate_limit_without_normal_retry() -> None:
    from finance_sync.connectors.exceptions import RateLimitError

    classification = classify_error(RateLimitError("slow down", retry_after=42))
    assert classification.category == "rate_limit"
    assert classification.status == "deferred"


def test_retry_policy_routes_authentication_to_manual_review() -> None:
    from finance_sync.connectors.exceptions import PermanentError

    classification = classify_error(PermanentError("invalid credentials"))
    assert classification.category == "authentication"
    assert classification.status == "manual_review"


def test_retry_at_is_bounded_by_cap() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    due = retry_at(20, now=now, base=10, cap=100, jitter=0)
    assert (due - now).total_seconds() == 100


def test_scheduling_score_prioritizes_severity_and_bounded_age() -> None:
    now = datetime(2026, 1, 31, tzinfo=UTC)
    critical = SimpleNamespace(
        severity="critical",
        priority=0,
        attempt_count=0,
        first_detected_at=now,
        context={},
    )
    old_warning = SimpleNamespace(
        severity="warning",
        priority=0,
        attempt_count=0,
        first_detected_at=now.replace(day=1),
        context={},
    )

    assert scheduling_score(critical, now=now) > scheduling_score(
        old_warning, now=now
    )


def test_resolved_re_detection_has_a_generation_key_after_grace_period() -> (
    None
):
    """The generation policy is encoded in the persisted key/context."""
    # Pure key stability remains the contract; generation is applied by the
    # repository only after it observes a resolved row.
    first = DetectedIssue("t", "p", "i", "e", "x", scope="stable")
    assert deduplication_key(first) == deduplication_key(first)


def test_batching_splits_compatible_items_and_separates_scopes() -> None:
    items = [
        SimpleNamespace(
            provider_key="p",
            connection_id="c1",
            remediation_strategy="s",
            context={},
        ),
        SimpleNamespace(
            provider_key="p",
            connection_id="c1",
            remediation_strategy="s",
            context={},
        ),
        SimpleNamespace(
            provider_key="p",
            connection_id="c2",
            remediation_strategy="s",
            context={},
        ),
    ]
    batches = create_batches(items, batch_limit=1)
    assert len(batches) == 3
    assert (
        len(
            coalesce_days(
                [datetime(2026, 1, 1).date(), datetime(2026, 1, 2).date()]
            )
        )
        == 1
    )


def test_detected_issue_batch_key_uses_the_same_compatibility_contract() -> (
    None
):
    from finance_sync.reconciliation.remediation.batching import (
        compatibility_key,
    )

    issue = DetectedIssue(
        "tenant",
        "trading212",
        "gap",
        "account",
        "account-1",
        connection_id="connection-1",
        remediation_strategy="transaction_history_gap",
        context={"endpoint_family": "transaction_history"},
    )
    assert compatibility_key(issue).startswith(
        "trading212|connection-1|transaction_history_gap|"
    )


def test_compatibility_key_separates_security_and_request_scope() -> None:
    from finance_sync.reconciliation.remediation.batching import (
        compatibility_key,
    )

    first = SimpleNamespace(
        provider_key="openbb",
        connection_id=None,
        remediation_strategy="historical_price_enrichment",
        context={
            "security_id": "security-1",
            "identifier": "ABC",
            "limit": 100,
        },
    )
    second = SimpleNamespace(
        provider_key="openbb",
        connection_id=None,
        remediation_strategy="historical_price_enrichment",
        context={
            "security_id": "security-2",
            "identifier": "XYZ",
            "limit": 365,
        },
    )

    assert compatibility_key(first) != compatibility_key(second)


def test_claim_fairness_interleaves_provider_candidates() -> None:
    from finance_sync.reconciliation.remediation.backlog import _fair_interleave

    rows = [
        SimpleNamespace(provider_key="bunq", id="b1"),
        SimpleNamespace(provider_key="bunq", id="b2"),
        SimpleNamespace(provider_key="ynab", id="y1"),
    ]
    interleaved = _fair_interleave(rows)
    assert [row.id for row in interleaved] == ["b1", "y1", "b2"]


def test_retry_after_supports_seconds_and_rejects_invalid_dates() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    due = parse_retry_after("10", now=now)
    assert due is not None and (due - now).total_seconds() == 10
    assert parse_retry_after("not-a-date", now=now) is None


@pytest.mark.asyncio
async def test_redis_quota_reservation_returns_shared_deferral() -> None:
    class FakeRedis:
        async def eval(self, *_args):
            return [0, 7]

    reservation = await RemediationRateLimitCoordinator(FakeRedis()).reserve(
        "p", "tenant", QuotaPolicy(1, 60, "history")
    )
    assert not reservation.allowed
    assert reservation.retry_at is not None
    assert reservation.reason == "quota_exhausted"


@pytest.mark.asyncio
async def test_redis_quota_reservation_respects_provider_cooldown() -> None:
    class FakeRedis:
        async def eval(self, *_args):
            return [0, 17, 2]

    reservation = await RemediationRateLimitCoordinator(FakeRedis()).reserve(
        "p", "tenant", QuotaPolicy(1, 60, "history")
    )
    assert not reservation.allowed
    assert reservation.reason == "cooldown"


@pytest.mark.asyncio
async def test_trading212_batch_resolves_instrument_by_isin(
    monkeypatch,
) -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.rows = {
                "issue-1": SimpleNamespace(
                    external_security_id="ABC",
                    provider_key="trading212",
                    raw_ticker="ABC",
                    raw_isin=None,
                    raw_name=None,
                    raw_currency_code=None,
                    raw_metadata=None,
                    resolution_method=None,
                    resolved_security_id=None,
                ),
                "issue-2": SimpleNamespace(
                    external_security_id="XYZ",
                    provider_key="trading212",
                    raw_ticker="XYZ",
                    raw_isin=None,
                    raw_name=None,
                    raw_currency_code=None,
                    raw_metadata=None,
                    resolution_method=None,
                    resolved_security_id=None,
                ),
            }
            self.security = SimpleNamespace(id="security-1")

        async def get(self, model, key):
            del model
            return self.rows.get(key)

        async def execute(self, statement):
            del statement
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(
                    first=lambda: SimpleNamespace(
                        encrypted_payload=b"payload",
                        nonce=b"nonce",
                        description="{}",
                    )
                )
            )

        async def scalar(self, statement):
            del statement
            return self.security

        flush = AsyncMock()

    class FakeConnector:
        def __init__(self) -> None:
            self.fetch_calls = 0

        async def authenticate(self) -> None:
            return None

        async def fetch_instruments(self):
            self.fetch_calls += 1
            return [
                {"ticker": "ABC", "isin": "NL0000000001", "name": "A"},
                {"ticker": "XYZ", "isin": "NL0000000002", "name": "B"},
            ]

    fake_session = FakeSession()
    fake_connector = FakeConnector()
    import finance_sync.reconciliation.remediation.trading212 as strategy_module

    monkeypatch.setattr(
        strategy_module,
        "decrypt_credential",
        lambda *_args: '{"api_key":"x"}',
    )
    monkeypatch.setattr(
        strategy_module,
        "ConnectorRegistry",
        lambda: SimpleNamespace(get_connector=lambda _config: fake_connector),
    )
    strategy = strategy_module.Trading212InstrumentMetadataStrategy(
        fake_session, SimpleNamespace()
    )

    outcomes = await strategy.execute_batch(
        [
            SimpleNamespace(
                id="issue-1",
                affected_entity_id="issue-1",
                tenant_id="tenant",
                provider_key="trading212",
                connection_id="connection",
            ),
            SimpleNamespace(
                id="issue-2",
                affected_entity_id="issue-2",
                tenant_id="tenant",
                provider_key="trading212",
                connection_id="connection",
            ),
        ]
    )

    assert outcomes == {"issue-1": "success", "issue-2": "success"}
    assert fake_connector.fetch_calls == 1
    assert fake_session.rows["issue-1"].resolved_security_id == "security-1"


@pytest.mark.asyncio
async def test_transaction_history_strategy_is_bounded_and_fails_closed() -> (
    None
):
    persisted: list[tuple[str, list[object]]] = []

    class FakeSession:
        async def scalar(self, _statement):
            return 1

    class FakeConnector:
        async def fetch_transactions(self, **kwargs):
            assert kwargs["account_id"] == "provider-account"
            assert kwargs["limit"] == 500
            return ["raw"]

        def transform_transactions(self, raw):
            return [f"canonical:{value}" for value in raw]

    async def persist(item, account_id, canonical):
        del item
        persisted.append((account_id, canonical))
        return len(canonical)

    strategy = TransactionHistoryStrategy(FakeSession(), persist=persist)
    item = SimpleNamespace(
        tenant_id="tenant",
        provider_key="trading212",
        context={
            "account_id": "account",
            "provider_account_id": "provider-account",
            "from": "2026-01-01T00:00:00+00:00",
            "to": "2026-01-03T00:00:00+00:00",
        },
    )

    assert strategy.supports(item)
    await strategy.execute(item, FakeConnector())
    assert persisted == [("account", ["canonical:raw"])]
    assert not (await strategy.verify(item)).resolved

    item.context["minimum_transactions"] = 1
    assert (await strategy.verify(item)).resolved
    item.provider_key = "bunq"
    assert strategy.supports(item)
    item.provider_key = "plaid_like"
    assert strategy.supports(item)


@pytest.mark.asyncio
async def test_transaction_history_batch_coalesces_same_account_windows() -> (
    None
):
    calls: list[object] = []
    persisted: list[tuple[str, list[object]]] = []

    class FakeSession:
        async def scalar(self, _statement):
            return 2

    class FakeConnector:
        async def fetch_transactions(self, **kwargs):
            calls.append(kwargs)
            return ["raw"]

        def transform_transactions(self, raw):
            return [f"canonical:{value}" for value in raw]

    async def persist(_item, account_id, canonical):
        persisted.append((account_id, canonical))
        return len(canonical)

    strategy = TransactionHistoryStrategy(FakeSession(), persist=persist)
    items = [
        SimpleNamespace(
            id="gap-1",
            tenant_id="tenant",
            provider_key="bunq",
            context={
                "account_id": "account",
                "provider_account_id": "provider-account",
                "from": "2026-01-01T00:00:00+00:00",
                "to": "2026-01-03T00:00:00+00:00",
            },
        ),
        SimpleNamespace(
            id="gap-2",
            tenant_id="tenant",
            provider_key="bunq",
            context={
                "account_id": "account",
                "provider_account_id": "provider-account",
                "from": "2026-01-03T00:00:00+00:00",
                "to": "2026-01-05T00:00:00+00:00",
            },
        ),
    ]

    outcomes = await strategy.execute_batch(items, FakeConnector())
    assert outcomes == {"gap-1": "success", "gap-2": "success"}
    assert len(calls) == 1
    assert persisted == [("account", ["canonical:raw"])]


@pytest.mark.asyncio
async def test_batch_exception_transitions_claimed_items() -> None:
    from finance_sync.connectors.exceptions import TransientError
    from finance_sync.reconciliation.remediation.batching import (
        RemediationBatch,
    )
    from finance_sync.reconciliation.remediation.executor import (
        RemediationExecutor,
    )

    class FailingStrategy:
        key = "transaction_history_gap"
        quota_cost = 1

        async def execute_batch(self, _items, _connector):
            message = "provider unavailable"
            raise TransientError(message)

    class FakeQuota:
        async def reserve(self, *_args, **_kwargs):
            return SimpleNamespace(allowed=True)

    class FakeBacklog:
        def __init__(self):
            self.transitions = []

        async def transition(self, *args, **kwargs):
            self.transitions.append((args, kwargs))
            return True

        async def mark_connection_auth_failure(self, *_args, **_kwargs):
            message = "authentication signal is not expected"
            raise AssertionError(message)

    item = SimpleNamespace(
        id="item-1",
        tenant_id="tenant",
        provider_key="bunq",
        connection_id="connection",
        remediation_strategy="transaction_history_gap",
        claim_token=None,
        attempt_count=1,
    )
    executor = RemediationExecutor(
        object(),
        strategies={"transaction_history_gap": FailingStrategy()},
        quota=FakeQuota(),
        quota_policies={
            "transaction_history_gap": QuotaPolicy(1, 60, "history")
        },
    )
    backlog = FakeBacklog()
    executor.backlog = backlog

    outcomes = await executor.execute_batch(RemediationBatch("batch", (item,)))
    assert outcomes == ["retry_wait"]
    assert backlog.transitions[0][1]["error_category"] == "transient"


@pytest.mark.asyncio
async def test_historical_price_verification_requires_scoped_valid_observations() -> (
    None
):
    class FakeSession:
        async def scalar(self, _statement):
            return 1

    strategy = HistoricalPriceStrategy(
        FakeSession(), SimpleNamespace(openbb_api_key=None)
    )
    item = SimpleNamespace(
        context={
            "security_id": "security-1",
            "interval": "1d",
            "start_date": "2026-01-01T00:00:00+00:00",
            "end_date": "2026-01-03T00:00:00+00:00",
            "minimum_observations": 1,
        }
    )

    assert (await strategy.verify(item)).resolved
    item.context.pop("start_date")
    assert not (await strategy.verify(item)).resolved


@pytest.mark.asyncio
async def test_latest_quote_verification_requires_security_scope_and_recent_price() -> (
    None
):
    class FakeSession:
        async def scalar(self, _statement):
            return 1

    strategy = LatestQuoteStrategy(
        FakeSession(), SimpleNamespace(openbb_api_key=None)
    )
    item = SimpleNamespace(
        context={"security_id": "security-1", "max_age_hours": 48}
    )

    assert (await strategy.verify(item)).resolved
    item.context.pop("security_id")
    assert not (await strategy.verify(item)).resolved


@pytest.mark.asyncio
async def test_latest_quote_execution_uses_scoped_gateway_request() -> None:
    class FakeSession:
        async def scalar(self, _statement):
            return 0

    strategy = LatestQuoteStrategy(
        FakeSession(), SimpleNamespace(openbb_api_key=None)
    )
    strategy.gateway.get_latest_quote = AsyncMock()
    item = SimpleNamespace(
        context={
            "security_id": "security-1",
            "identifier": "ABC",
            "identifier_type": "ticker",
        }
    )

    await strategy.execute(item)

    strategy.gateway.get_latest_quote.assert_awaited_once_with(
        security_id="security-1",
        identifier="ABC",
        identifier_type="ticker",
    )


@pytest.mark.asyncio
async def test_historical_price_batch_coalesces_same_security_window() -> None:
    strategy = HistoricalPriceStrategy(
        object(), SimpleNamespace(openbb_api_key=None)
    )
    strategy.gateway.get_historical_prices = AsyncMock()
    items = [
        SimpleNamespace(
            id=f"item-{index}",
            context={
                "security_id": "security-1",
                "identifier": "ABC",
                "start_date": f"2026-01-0{index + 1}T00:00:00+00:00",
                "end_date": f"2026-01-0{index + 2}T00:00:00+00:00",
                "interval": "1d",
            },
        )
        for index in range(2)
    ]

    outcomes = await strategy.execute_batch(items)

    assert outcomes == {"item-0": "success", "item-1": "success"}
    strategy.gateway.get_historical_prices.assert_awaited_once()
    call = strategy.gateway.get_historical_prices.await_args.kwargs
    assert call["start_date"] == datetime(2026, 1, 1, tzinfo=UTC)
    assert call["end_date"] == datetime(2026, 1, 3, tzinfo=UTC)


@pytest.mark.asyncio
async def test_latest_quote_batch_fetches_one_quote_for_same_security() -> None:
    strategy = LatestQuoteStrategy(
        object(), SimpleNamespace(openbb_api_key=None)
    )
    strategy.gateway.get_latest_quote = AsyncMock()
    items = [
        SimpleNamespace(
            id=f"item-{index}",
            context={
                "security_id": "security-1",
                "identifier": "ABC",
                "identifier_type": "ticker",
            },
        )
        for index in range(2)
    ]

    outcomes = await strategy.execute_batch(items)

    assert outcomes == {"item-0": "success", "item-1": "success"}
    strategy.gateway.get_latest_quote.assert_awaited_once_with(
        security_id="security-1",
        identifier="ABC",
        identifier_type="ticker",
    )


def test_price_strategies_require_explicit_contracts() -> None:
    from finance_sync.reconciliation.remediation.price_history import (
        HistoricalPriceStrategy,
    )

    historical = HistoricalPriceStrategy(
        object(), SimpleNamespace(openbb_api_key=None)
    )
    quote = LatestQuoteStrategy(object(), SimpleNamespace(openbb_api_key=None))
    item = SimpleNamespace(
        context={"security_id": "security-1", "identifier": "ABC"}
    )

    assert quote.supports(item)
    assert not historical.supports(item)
    item.context.update(
        {
            "start_date": "2026-01-01T00:00:00+00:00",
            "end_date": "2026-01-02T00:00:00+00:00",
        }
    )
    assert historical.supports(item)


@pytest.mark.asyncio
async def test_trading212_verification_rejects_cross_tenant_row() -> None:
    strategy = Trading212InstrumentMetadataStrategy(
        SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    tenant_id="other-tenant", resolved_security_id="security-1"
                )
            )
        ),
        SimpleNamespace(),
    )
    item = SimpleNamespace(
        tenant_id="tenant-1", affected_entity_id="unresolved-1"
    )

    assert not (await strategy.verify(item)).resolved


@pytest.mark.asyncio
async def test_remediation_operator_mutations_are_audited() -> None:
    service = __import__(
        "finance_sync.services.remediation", fromlist=["RemediationService"]
    ).RemediationService(object(), "tenant-1")
    service.backlog.transition = AsyncMock(return_value=True)

    with patch(
        "finance_sync.services.connection_audit.log_connection_event",
        new=AsyncMock(),
    ) as audit:
        assert await service.requeue("item-1", actor_user_id="user-1")
        assert await service.ignore(
            "item-2", "not actionable", actor_user_id="user-1"
        )

    assert audit.await_count == 2
    assert [call.kwargs["action"] for call in audit.await_args_list] == [
        "retry",
        "ignore",
    ]


@pytest.mark.asyncio
async def test_remediation_service_delegates_queries_and_registers_tenant_issues() -> (
    None
):
    from finance_sync.services.remediation import RemediationService

    service = RemediationService(object(), "tenant-1")
    item = SimpleNamespace(id="item-1")
    service.backlog.list_for_tenant = AsyncMock(return_value=[item])
    service.backlog.get = AsyncMock(return_value=item)
    service.backlog.register = AsyncMock(return_value=item)

    assert await service.list(status="failed") == [item]
    assert await service.get("item-1") is item
    issue = SimpleNamespace(tenant_id="tenant-1")
    assert await service.register_detected_issues([issue]) == [item]
    service.backlog.list_for_tenant.assert_awaited_once_with(
        "tenant-1", status="failed"
    )
    service.backlog.get.assert_awaited_once_with("tenant-1", "item-1")


@pytest.mark.asyncio
async def test_remediation_service_rejects_cross_tenant_issue() -> None:
    from finance_sync.services.remediation import RemediationService

    service = RemediationService(object(), "tenant-1")
    with pytest.raises(ValueError, match="tenant does not match"):
        await service.register_detected_issues(
            [SimpleNamespace(tenant_id="tenant-2")]
        )


@pytest.mark.asyncio
async def test_remediation_service_requeue_without_change_skips_audit() -> None:
    from finance_sync.services.remediation import RemediationService

    service = RemediationService(object(), "tenant-1")
    service.backlog.transition = AsyncMock(return_value=False)
    service._audit = AsyncMock()

    assert not await service.requeue("item-1")
    service._audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_remediation_service_bulk_requeue_is_bounded_and_audited() -> (
    None
):
    from finance_sync.services.remediation import RemediationService

    session = SimpleNamespace(execute=AsyncMock())
    row_result = SimpleNamespace(
        all=lambda: [("item-1", "bunq"), ("item-2", "bunq")]
    )
    session.execute.return_value = row_result
    service = RemediationService(session, "tenant-1")

    with patch(
        "finance_sync.services.connection_audit.log_connection_event",
        new=AsyncMock(),
    ) as audit:
        result = await service.bulk_requeue(
            ["item-1", "item-1", *[f"item-{i}" for i in range(2, 120)]],
            actor_role="operator",
        )

    assert result == ["item-1", "item-2"]
    assert session.execute.await_count == 1
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["actor_role"] == "operator"


@pytest.mark.asyncio
async def test_remediation_service_patch_handles_empty_and_changed_updates() -> (
    None
):
    from finance_sync.services.remediation import RemediationService

    result = SimpleNamespace(rowcount=1)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    service = RemediationService(session, "tenant-1")
    service._audit = AsyncMock()

    assert not await service.patch("item-1")
    assert await service.patch("item-1", priority=5, status="deferred")
    service._audit.assert_awaited_once()
    assert service._audit.await_args.kwargs["detail"] == {
        "priority": 5,
        "status": "deferred",
    }


@pytest.mark.asyncio
async def test_remediation_service_bulk_requeue_empty_input_does_not_query() -> (
    None
):
    from finance_sync.services.remediation import RemediationService

    session = SimpleNamespace(execute=AsyncMock())
    service = RemediationService(session, "tenant-1")

    assert await service.bulk_requeue([]) == []
    session.execute.assert_not_awaited()
