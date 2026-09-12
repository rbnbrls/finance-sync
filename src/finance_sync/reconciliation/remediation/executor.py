"""Executor boundary for provider calls.

Strategies are injected, making it impossible for the detector or planner to
accidentally call a provider.  An absent strategy is manual review.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Protocol, cast

import structlog

from finance_sync.connectors.exceptions import RateLimitError
from finance_sync.observability.metrics import (
    data_quality_remediation_api_calls_saved_total,
    data_quality_remediation_attempts_total,
    data_quality_remediation_average_items_per_batch,
    data_quality_remediation_batch_items_total,
    data_quality_remediation_batches_created_total,
    data_quality_remediation_deferred_rate_limit_total,
    data_quality_remediation_failures_total,
    data_quality_remediation_throughput_total,
)
from finance_sync.reconciliation.remediation.backlog import BacklogRepository
from finance_sync.reconciliation.remediation.policies import (
    classify_error,
    retry_at,
)
from finance_sync.reconciliation.remediation.rate_limit import (
    parse_retry_after,
)


class RemediationStrategy(Protocol):
    """Typed boundary implemented by provider remediation strategies."""

    key: str
    quota_cost: int

    async def execute(
        self, item: DataQualityRemediationItem, connector: Any = None
    ) -> None: ...

    async def verify(self, item: DataQualityRemediationItem) -> Any: ...


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from finance_sync.models.remediation import DataQualityRemediationItem
    from finance_sync.reconciliation.remediation.batching import (
        RemediationBatch,
    )
    from finance_sync.reconciliation.remediation.rate_limit import (
        QuotaPolicy,
        RemediationRateLimitCoordinator,
    )


logger = structlog.get_logger("finance_sync.remediation.executor")


class RemediationExecutor:
    def __init__(
        self,
        session: AsyncSession,
        strategies: dict[str, RemediationStrategy] | None = None,
        *,
        quota: RemediationRateLimitCoordinator | None = None,
        quota_policies: dict[str, QuotaPolicy] | None = None,
        max_verification_attempts: int = 3,
        max_execution_seconds: int = 300,
        retry_base_seconds: float = 30.0,
        retry_cap_seconds: float = 3600.0,
        retry_jitter: float = 0.2,
        connector_factory: Any | None = None,
    ) -> None:
        self.backlog = BacklogRepository(session)
        self.strategies = strategies or {}
        self.quota = quota
        self.quota_policies = quota_policies or {}
        self.max_verification_attempts = max_verification_attempts
        self.max_execution_seconds = max(1, max_execution_seconds)
        self.retry_base_seconds = retry_base_seconds
        self.retry_cap_seconds = retry_cap_seconds
        self.retry_jitter = retry_jitter
        self.connector_factory = connector_factory

    def _retry_at(self, attempt: int) -> datetime:
        return retry_at(
            attempt,
            base=self.retry_base_seconds,
            cap=self.retry_cap_seconds,
            jitter=self.retry_jitter,
        )

    async def execute(
        self, item: DataQualityRemediationItem, *, connector: Any = None
    ) -> str:
        strategy = self.strategies.get(item.remediation_strategy)
        supports: Any = getattr(strategy, "supports", None)
        if strategy is None or (callable(supports) and not supports(item)):
            await self.backlog.transition(
                str(item.tenant_id),
                str(item.id),
                status="manual_review",
                claim_token=item.claim_token,
                error="No remediation strategy is registered",
                error_category="unsupported",
            )
            data_quality_remediation_throughput_total.labels(
                provider=str(item.provider_key),
                strategy=str(item.remediation_strategy),
                outcome="manual_review",
            ).inc()
            return "manual_review"
        policy: QuotaPolicy | None = None
        owned_connector = False
        try:
            if connector is None and self.connector_factory is not None:
                connector = await self.connector_factory(item)
                owned_connector = connector is not None
            policy = self.quota_policies.get(item.remediation_strategy)
            if self.quota is None or policy is None:
                await self.backlog.transition(
                    str(item.tenant_id),
                    str(item.id),
                    status="deferred",
                    claim_token=item.claim_token,
                    error_category="quota_policy_missing",
                    next_attempt_at=self._retry_at(item.attempt_count),
                )
                return "deferred"
            reservation = await self.quota.reserve(
                str(item.provider_key),
                str(item.connection_id or item.tenant_id),
                policy,
                cost=getattr(strategy, "quota_cost", 1),
            )
            if not reservation.allowed:
                await self.backlog.transition(
                    str(item.tenant_id),
                    str(item.id),
                    status="deferred",
                    claim_token=item.claim_token,
                    error_category="rate_limit",
                    next_attempt_at=reservation.retry_at
                    or self._retry_at(item.attempt_count),
                    increment_rate_limit_deferrals=True,
                )
                data_quality_remediation_deferred_rate_limit_total.labels(
                    provider=str(item.provider_key),
                    endpoint_family=policy.endpoint_family,
                ).inc()
                return "deferred"
            data_quality_remediation_attempts_total.labels(
                provider=str(item.provider_key),
                strategy=str(item.remediation_strategy),
            ).inc()
            async with asyncio.timeout(self.max_execution_seconds):
                await strategy.execute(item, connector)
                if str(getattr(strategy, "key", "")).startswith("wealthfolio_"):
                    verification: Any = await cast("Any", strategy).verify(
                        item, connector
                    )
                else:
                    verification = await cast("Any", strategy).verify(item)
            item.verification_count += 1
            _record_verification(item, verification)
            status = (
                "resolved"
                if verification.resolved
                else (
                    "manual_review"
                    if item.verification_count >= self.max_verification_attempts
                    else "retry_wait"
                )
            )
            scheduled_next_attempt = (
                self._retry_at(item.attempt_count)
                if status != "resolved"
                else None
            )
            await self.backlog.transition(
                str(item.tenant_id),
                str(item.id),
                status=status,
                claim_token=item.claim_token,
                next_attempt_at=scheduled_next_attempt,
            )
            logger.info(
                "remediation_item_outcome",
                backlog_item_id=str(item.id),
                provider=str(item.provider_key),
                connection_id=str(item.connection_id or "-")[:128],
                strategy=str(item.remediation_strategy),
                attempt=item.attempt_count,
                classification="resolved" if status == "resolved" else status,
                next_attempt_at=(
                    scheduled_next_attempt.isoformat()
                    if scheduled_next_attempt is not None
                    else None
                ),
                claim_token_hash=_claim_token_hash(item.claim_token),
            )
            data_quality_remediation_throughput_total.labels(
                provider=str(item.provider_key),
                strategy=str(item.remediation_strategy),
                outcome=status,
            ).inc()
            return status
        except Exception as exc:
            classification = classify_error(exc)
            next_attempt = self._retry_at(item.attempt_count)
            if classification.category == "authentication":
                await self.backlog.mark_connection_auth_failure(
                    str(item.tenant_id),
                    item.connection_id,
                    error="remediation authentication failed",
                )
            if isinstance(exc, RateLimitError) and exc.retry_after is not None:
                next_attempt = (
                    parse_retry_after(exc.retry_after) or next_attempt
                )
                if self.quota is not None and policy is not None:
                    await self.quota.cooldown(
                        str(item.provider_key),
                        str(item.connection_id or item.tenant_id),
                        policy.endpoint_family,
                        next_attempt,
                    )
            await self.backlog.transition(
                str(item.tenant_id),
                str(item.id),
                status=classification.status,
                claim_token=item.claim_token,
                error=str(exc)[:512],
                error_category=classification.category,
                next_attempt_at=next_attempt,
                increment_rate_limit_deferrals=isinstance(exc, RateLimitError),
            )
            logger.warning(
                "remediation_item_failed",
                backlog_item_id=str(item.id),
                provider=str(item.provider_key),
                connection_id=str(item.connection_id or "-")[:128],
                strategy=str(item.remediation_strategy),
                attempt=item.attempt_count,
                classification=classification.category,
                next_attempt_at=next_attempt.isoformat(),
                claim_token_hash=_claim_token_hash(item.claim_token),
            )
            data_quality_remediation_throughput_total.labels(
                provider=str(item.provider_key),
                strategy=str(item.remediation_strategy),
                outcome=classification.category,
            ).inc()
            data_quality_remediation_failures_total.labels(
                provider=str(item.provider_key),
                strategy=str(item.remediation_strategy),
                category=classification.category,
            ).inc()
            return classification.status
        finally:
            if owned_connector and connector is not None:
                close = getattr(connector, "close", None)
                if callable(close):
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result

    async def execute_batch(self, batch: RemediationBatch) -> list[str]:
        """Process a compatibility batch with per-item verification."""
        if not batch.items:
            return []
        data_quality_remediation_batches_created_total.inc()
        data_quality_remediation_batch_items_total.inc(len(batch.items))
        data_quality_remediation_average_items_per_batch.set(len(batch.items))
        first = batch.items[0]
        strategy = self.strategies.get(first.remediation_strategy)
        if strategy is None:
            return [await self.execute(item) for item in batch.items]
        batch_method = getattr(strategy, "execute_batch", None)
        if batch_method is None:
            return [await self.execute(item) for item in batch.items]
        policy = self.quota_policies.get(first.remediation_strategy)
        if self.quota is None or policy is None:
            return [await self.execute(item) for item in batch.items]
        batch_id = _batch_id(batch)
        reservation = await self.quota.reserve(
            str(first.provider_key),
            str(first.connection_id or first.tenant_id),
            policy,
            cost=getattr(strategy, "quota_cost", 1),
        )
        if not reservation.allowed:
            for item in batch.items:
                await self.backlog.transition(
                    str(item.tenant_id),
                    str(item.id),
                    status="deferred",
                    claim_token=item.claim_token,
                    error_category="rate_limit",
                    next_attempt_at=reservation.retry_at
                    or self._retry_at(item.attempt_count),
                    increment_rate_limit_deferrals=True,
                )
            return ["deferred"] * len(batch.items)
        try:
            data_quality_remediation_attempts_total.labels(
                provider=str(first.provider_key),
                strategy=str(first.remediation_strategy),
            ).inc()
            async with asyncio.timeout(self.max_execution_seconds):
                outcomes: dict[str, str] = await batch_method(
                    list(batch.items), None
                )
        except TimeoutError:
            return await self._transition_batch_failure(
                batch, TimeoutError("remediation execution timed out")
            )
        except Exception as exc:
            return await self._transition_batch_failure(batch, exc)
        data_quality_remediation_api_calls_saved_total.inc(
            max(0, len(batch.items) - 1)
        )
        results: list[str] = []
        logger.info(
            "remediation_batch_execution",
            batch_id=batch_id,
            provider=str(first.provider_key),
            connection_id=str(first.connection_id or "-")[:128],
            strategy=str(first.remediation_strategy),
            batch_size=len(batch.items),
            claim_token_hashes=[
                _claim_token_hash(item.claim_token) for item in batch.items
            ],
        )
        for item in batch.items:
            if outcomes.get(str(item.id)) != "success":
                await self.backlog.transition(
                    str(item.tenant_id),
                    str(item.id),
                    status="retry_wait",
                    claim_token=item.claim_token,
                    error_category="partial_failure",
                    next_attempt_at=self._retry_at(item.attempt_count),
                )
                results.append("retry_wait")
                continue
            verification = await strategy.verify(item)
            item.verification_count += 1
            _record_verification(item, verification)
            status = (
                "resolved"
                if verification.resolved
                else (
                    "manual_review"
                    if item.verification_count >= self.max_verification_attempts
                    else "retry_wait"
                )
            )
            await self.backlog.transition(
                str(item.tenant_id),
                str(item.id),
                status=status,
                claim_token=item.claim_token,
                next_attempt_at=self._retry_at(item.attempt_count)
                if status == "retry_wait"
                else None,
            )
            results.append(status)
        return results

    async def _transition_batch_failure(
        self, batch: RemediationBatch, exc: Exception
    ) -> list[str]:
        """Resolve a batch exception without leaving claims stuck processing."""
        classification = classify_error(exc)
        first = batch.items[0]
        next_attempt = self._retry_at(first.attempt_count)
        policy = self.quota_policies.get(first.remediation_strategy)
        if classification.category == "authentication":
            await self.backlog.mark_connection_auth_failure(
                str(first.tenant_id),
                first.connection_id,
                error="remediation authentication failed",
            )
        if isinstance(exc, RateLimitError) and exc.retry_after is not None:
            next_attempt = parse_retry_after(exc.retry_after) or next_attempt
            if self.quota is not None and policy is not None:
                await self.quota.cooldown(
                    str(first.provider_key),
                    str(first.connection_id or first.tenant_id),
                    policy.endpoint_family,
                    next_attempt,
                )
        for item in batch.items:
            await self.backlog.transition(
                str(item.tenant_id),
                str(item.id),
                status=classification.status,
                claim_token=item.claim_token,
                error=str(exc)[:512],
                error_category=classification.category,
                next_attempt_at=next_attempt,
                increment_rate_limit_deferrals=isinstance(exc, RateLimitError),
            )
            data_quality_remediation_failures_total.labels(
                provider=str(item.provider_key),
                strategy=str(item.remediation_strategy),
                category=classification.category,
            ).inc()
        logger.warning(
            "remediation_batch_failed",
            batch_id=_batch_id(batch),
            batch_size=len(batch.items),
            provider=str(first.provider_key),
            strategy=str(first.remediation_strategy),
            classification=classification.category,
            next_attempt_at=next_attempt.isoformat(),
        )
        return [classification.status] * len(batch.items)


def _claim_token_hash(token: Any) -> str | None:
    if token is None:
        return None
    return sha256(str(token).encode()).hexdigest()[:16]


def _batch_id(batch: RemediationBatch) -> str:
    return sha256(
        "|".join(str(item.id) for item in batch.items).encode()
    ).hexdigest()[:16]


def _record_verification(
    item: DataQualityRemediationItem, verification: Any
) -> None:
    """Persist bounded verification evidence without provider payloads."""
    item.verified_at = datetime.now(UTC)
    reason = getattr(verification, "reason", None)
    if isinstance(reason, str):
        item.context = {
            **item.context,
            "last_verification_reason": reason[:256],
        }
