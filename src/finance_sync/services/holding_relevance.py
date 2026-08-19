"""Holding-relevance service: match, cluster, rank, acknowledge, correct.

Implements backlog/plus-relevant-nieuws-en-events.md.  The service is
**deterministic**: the security match, holding status, dates and source
references are finance-sync facts computed from canonical rows — never
invented by an LLM.  Hermes may later *explain* relevance in a few
sentences, but it can only cite the rows this service writes.

Pipeline
--------

1. :meth:`HoldingRelevanceService.build_feed` — for every tenant,
   match unresolved market-intelligence observations (``security_id``
   already resolved by the intel layer) against the tenant's **current
   holdings** (latest snapshot per account+security with quantity > 0)
   and **recently sold** holdings (latest snapshot per security with
   quantity == 0 / no current position), then cluster and rank.

2. Clustering — syndicated coverage of the same event merges into one
   story keyed on ``security_id + event_type + event_date`` (day
   granularity for plain news).  Distinct events (different quarter,
   ex-date vs payment date) always produce distinct clusters.

3. Ranking — deterministic score: holding weight x event proximity x
   recency x source reliability, normalised to 0..1.

4. Feed — tenant + user scoped read with filters (security, account,
   item type, date, unread/acknowledged) and always-present source URL,
   published/fetched at and freshness.

5. Corrections — per-user false-positive suppression that never touches
   the underlying observation and never affects other tenants.

6. Notifications — opt-in, deduplicated per (user, cluster, event
   type), lockscreen-safe by default (no position sizes/values).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import and_, desc, func, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

# Make JSONB work with SQLite (same pattern as the repo's tests).
if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[assignment]

from finance_sync.models.account import Account
from finance_sync.models.holding import Holding
from finance_sync.models.holding_relevance import (
    CORRECTION_DISMISS,
    EVENT_TYPE_CURRENCY,
    EVENT_TYPE_INTEREST,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    HOLDING_STATUS_CURRENT,
    HOLDING_STATUS_RECENTLY_SOLD,
    MATCH_REASON_CURRENCY_INTEREST,
    MATCH_REASON_EXACT_SECURITY,
    MATCH_REASON_RECENTLY_SOLD,
    HoldingRelevanceItem,
    RelevanceAck,
    RelevanceCluster,
    RelevanceClusterItem,
    RelevanceCorrection,
    RelevanceNotificationLog,
    RelevanceNotificationPreference,
)
from finance_sync.models.market_intelligence_item import (
    MarketIntelligenceItem,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from finance_sync.db.uow import UnitOfWork

logger = structlog.get_logger(__name__)

#: How far back a "recently sold" holding still surfaces relevant news.
RECENTLY_SOLD_WINDOW = timedelta(days=180)

#: Freshness threshold: items fetched longer ago than this are stale.
STALE_AFTER = timedelta(hours=24)

#: Reliability weights per provider key (higher = more trustworthy).
_PROVIDER_RELIABILITY: dict[str, float] = {
    "sec": 1.0,
    "sec_press": 0.9,
    "openbb": 0.7,
}

#: Event-type → weight used for the calendar/feed badge.
_EVENT_TYPE_WEIGHT: dict[str, float] = {
    "earnings": 1.0,
    "dividend": 0.9,
    "agm": 0.7,
    "split": 0.8,
    "merger": 0.95,
    "acquisition": 0.95,
    "filing": 0.6,
    "news": 0.5,
    "interest": 0.6,
    "currency": 0.6,
}


def _fact_value(item: MarketIntelligenceItem, key: str) -> Any | None:
    """Return the first structured fact value for *key* (or None)."""
    for fact in item.facts or []:
        if fact.get("key") == key:
            return fact.get("value")
    return None


def _event_date_for(item: MarketIntelligenceItem) -> datetime | None:
    """Normalise the item's event date from structured facts.

    Priority: explicit event dates from facts (ex/record/payment date,
    meeting date, split date, filing date), then the published date.
    """
    if item.facts:
        for key in (
            "event_date",
            "ex_date",
            "record_date",
            "payment_date",
            "meeting_date",
            "split_date",
            "filing_date",
            "earnings_date",
        ):
            raw = _fact_value(item, key)
            if raw:
                parsed = _coerce_datetime(raw)
                if parsed is not None:
                    return parsed
    return item.published_at


def _coerce_datetime(value: Any) -> datetime | None:
    """Coerce a fact value to a timezone-aware datetime, or None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d",
            "%Y-%m-%d %H:%M:%S",
        ):
            parsed = _parse_dt(value, fmt)
            if parsed is not None:
                return parsed
    return None


def _parse_dt(value: str, fmt: str) -> datetime | None:
    """Parse *value* with *fmt*, attaching UTC to naive results."""
    try:
        parsed = datetime.strptime(value, fmt)  # noqa: DTZ007 — tz attached below
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime (SQLite round-trips drop tzinfo).

    PostgreSQL ``timestamptz`` preserves the offset, but the in-memory
    SQLite dialect used by the fast unit suite returns naive datetimes.
    Every datetime arithmetic site in this service goes through here so
    the code behaves identically on both backends.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _valid_uuid_or_none(value: str | None) -> str | None:
    """Return *value* when it is a well-formed UUID, else None.

    Security/account filter values are compared against UUID columns.
    Injection payloads and malformed ids must behave as data (match no
    rows) rather than raise in the dialect's UUID bind processor.
    """
    if value is None:
        return None
    import uuid as _uuid

    try:
        parsed = _uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None
    return str(parsed)


def _event_type_for(item: MarketIntelligenceItem) -> str:
    """Map an intel item kind + facts to a cluster event type."""
    kind = (item.kind or "news_article").lower()
    # Explicit structured event_type fact wins (cash interest/currency
    # events arrive as corporate_event/news_article kinds with facts).
    fact_type = _fact_value(item, "event_type")
    if isinstance(fact_type, str):
        fact_type = fact_type.lower()
        if fact_type in (
            "interest",
            "currency",
            "fx",
            "earnings",
            "dividend",
            "agm",
            "meeting",
            "split",
            "merger",
            "acquisition",
            "filing",
        ):
            if fact_type == "fx":
                return EVENT_TYPE_CURRENCY
            return fact_type
    if "interest" in kind or _fact_value(item, "interest_rate") is not None:
        return EVENT_TYPE_INTEREST
    if "currency" in kind or "fx" in kind or _fact_value(item, "currency_pair"):
        return EVENT_TYPE_CURRENCY
    if "earnings" in kind:
        return "earnings"
    if "dividend" in kind or _fact_value(item, "dividend"):
        return "dividend"
    if "agm" in kind or "meeting" in kind or _fact_value(item, "meeting_date"):
        return "agm"
    if "split" in kind or _fact_value(item, "split_date"):
        return "split"
    if "merger" in kind or "merger" in (item.headline or "").lower():
        return "merger"
    if "acquisition" in kind or "acquisition" in (item.headline or "").lower():
        return "acquisition"
    if "filing" in kind or "8-k" in (item.headline or "").lower():
        return "filing"
    return "news"


def _story_key(
    security_id: str | None,
    event_type: str,
    event_date: datetime | None,
) -> str:
    """Deterministic clustering identity."""
    date_part = event_date.date().isoformat() if event_date else "no-date"
    return f"{security_id or 'none'}:{event_type}:{date_part}"


def _reliability(provider: str) -> float:
    return _PROVIDER_RELIABILITY.get(provider, 0.5)


def _is_fresh(item: MarketIntelligenceItem, now: datetime) -> bool:
    if item.stale_after is not None:
        return datetime.now(UTC) < _as_utc(item.stale_after)  # type: ignore[operator]
    return (now - _as_utc(item.fetched_at)) <= STALE_AFTER  # type: ignore[operator]


class HoldingRelevanceService:
    """Match, cluster, rank and serve holding-relevant news/events."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    # ── Public: build feed (match + cluster + rank) ─────────────────

    async def build_feed(
        self,
        tenant_id: str,
        *,
        user_id: str | None = None,
        window: timedelta = timedelta(days=14),
        include_recently_sold: bool = True,
    ) -> dict[str, int]:
        """Match stored observations to the tenant's holdings and cluster.

        Idempotent: running twice never duplicates rows (unique
        constraints on (tenant, item, security) and (tenant, story_key)
        make re-matches no-ops).  ``user_id`` is accepted for symmetry
        with the feed read (per-user ack/correction overlay happens at
        read time); matching itself is tenant-wide.
        """
        del user_id  # matching is tenant-wide; per-user overlay happens on read
        now = datetime.now(UTC)
        holdings = await self._current_holdings(tenant_id)
        security_ids = {str(h.security_id) for h in holdings}
        recently_sold: set[str] = set()
        if include_recently_sold:
            recently_sold = await self._recently_sold_securities(tenant_id, now)
        # Cash/currency holdings map to no canonical security — they only
        # matter for interest/currency events via their account.  Include
        # the tenant's cash-type accounts (bunq savings/checking/cash) so
        # genuinely relevant interest/currency events still surface even
        # though cash accounts have no holding rows.
        account_ids = {str(h.account_id) for h in holdings}
        account_ids.update(await self._cash_account_ids(tenant_id))

        # Observations in the window.  Security-resolved ones match
        # holdings; security-less ones may be cash/currency events for
        # the tenant's cash accounts (decided in _match_cash_events).
        stmt = (
            select(MarketIntelligenceItem)
            .where(
                MarketIntelligenceItem.tenant_id == tenant_id,  # type: ignore[attr-defined]
                MarketIntelligenceItem.published_at >= now - window,  # type: ignore[attr-defined]
            )
            .order_by(desc(MarketIntelligenceItem.published_at))  # type: ignore[attr-defined]
        )
        rows = list((await self._uow.session.execute(stmt)).scalars().all())  # type: ignore[assignment]

        matched = 0
        for item in rows:
            if item.security_id is None:
                # No canonical security → only cash/currency relevance
                # applies, handled below.
                continue
            sec_id = str(item.security_id)
            if sec_id in security_ids:
                # Current holding → canonical security match.
                weight = await self._holding_weight(tenant_id, sec_id)
                await self._upsert_relevance(
                    tenant_id,
                    item,
                    sec_id,
                    account_id=None,
                    match_reason=MATCH_REASON_EXACT_SECURITY,
                    confidence=1.0,
                    holding_status=HOLDING_STATUS_CURRENT,
                    holding_weight=weight,
                )
                matched += 1
            elif sec_id in recently_sold:
                await self._upsert_relevance(
                    tenant_id,
                    item,
                    sec_id,
                    account_id=None,
                    match_reason=MATCH_REASON_RECENTLY_SOLD,
                    confidence=0.8,
                    holding_status=HOLDING_STATUS_RECENTLY_SOLD,
                    holding_weight=None,
                )
                matched += 1

        # Cash/currency interest events (bunq cash accounts): only match
        # when the item is content-relevant (interest/currency events).
        if account_ids:
            matched += await self._match_cash_events(
                tenant_id, account_ids, rows, now
            )

        clustered = await self._recluster(tenant_id)

        summary = {
            "matched": matched,
            "clustered": clustered,
            "current_holdings": len(security_ids),
            "recently_sold": len(recently_sold),
            "window_days": int(window.total_seconds() // 86400),
        }
        logger.info(
            "holding_relevance_build_feed",
            tenant_id=tenant_id,
            **summary,
        )
        return summary

    # ── Public: read feed ───────────────────────────────────────────

    async def feed(
        self,
        tenant_id: str,
        *,
        user_id: str | None = None,
        security_id: str | None = None,
        account_id: str | None = None,
        item_type: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        unread_only: bool = False,
        acknowledged: bool | None = None,
        include_stale: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return the ranked, clustered holding feed for a tenant.

        ``user_id`` enables per-user unread/acknowledged semantics;
        without it every cluster is served as unread (no per-user rows).
        Filters are applied as **data** (parameterised), never as SQL
        fragments.  Cross-tenant security/account ids simply match no
        rows — no error, no leak.
        """
        conditions: list[Any] = [
            RelevanceCluster.tenant_id == tenant_id,  # type: ignore[attr-defined]
        ]
        # Filter values are data, never SQL fragments.  A malformed /
        # non-UUID security/account id (e.g. injection payloads) matches
        # no rows instead of crashing the UUID bind processor.
        security_id = _valid_uuid_or_none(security_id)
        account_id = _valid_uuid_or_none(account_id)
        if security_id:
            conditions.append(
                RelevanceCluster.security_id == security_id  # type: ignore[attr-defined]
            )
        if item_type:
            conditions.append(
                RelevanceCluster.event_type == item_type  # type: ignore[attr-defined]
            )
        if date_from:
            conditions.append(
                RelevanceCluster.event_date >= date_from  # type: ignore[attr-defined]
            )
        if date_to:
            conditions.append(
                RelevanceCluster.event_date <= date_to  # type: ignore[attr-defined]
            )

        stmt = (
            select(RelevanceCluster)
            .where(*conditions)
            .order_by(
                desc(RelevanceCluster.score),  # type: ignore[attr-defined]
                desc(RelevanceCluster.event_date),  # type: ignore[attr-defined]
            )
        )
        rows = list(
            (await self._uow.session.execute(stmt)).scalars().all()  # type: ignore[assignment]
        )
        if account_id:
            rows = await self._filter_by_account(tenant_id, account_id, rows)

        # Per-user ack overlay.
        ack_map: dict[str, RelevanceAck] = {}
        if user_id:
            ack_map = await self._ack_map(
                tenant_id, user_id, [str(r.id) for r in rows]
            )

        # Project every matching cluster to its per-user DTO first, then
        # paginate.  ``total`` therefore reflects what THIS user actually
        # sees (corrections, stale suppression and ack filters all apply)
        # — never a raw DB count that would leak hidden items.
        visible: list[dict[str, Any]] = []
        for row in rows:
            dto = await self._cluster_to_dto(
                tenant_id,
                row,
                ack=ack_map.get(str(row.id)),
                user_id=user_id,
                include_stale=include_stale,
            )
            if dto is None:
                continue
            if unread_only and dto["acknowledged"]:
                continue
            if acknowledged is True and not dto["acknowledged"]:
                continue
            if acknowledged is False and dto["acknowledged"]:
                continue
            visible.append(dto)

        clusters = visible[offset : offset + limit]
        return {
            "items": clusters,
            "total": len(visible),
            "limit": limit,
            "offset": offset,
        }

    # ── Public: calendar ────────────────────────────────────────────

    async def calendar(
        self,
        tenant_id: str,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return upcoming/past event clusters for the calendar view."""
        conditions: list[Any] = [
            RelevanceCluster.tenant_id == tenant_id,  # type: ignore[attr-defined]
            RelevanceCluster.event_date.is_not(None),  # type: ignore[attr-defined]
        ]
        if date_from:
            conditions.append(
                RelevanceCluster.event_date >= date_from  # type: ignore[attr-defined]
            )
        if date_to:
            conditions.append(
                RelevanceCluster.event_date <= date_to  # type: ignore[attr-defined]
            )
        stmt = (
            select(RelevanceCluster)
            .where(*conditions)
            .order_by(RelevanceCluster.event_date.asc())  # type: ignore[attr-defined]
            .limit(limit)
        )
        rows = list(
            (await self._uow.session.execute(stmt)).scalars().all()  # type: ignore[assignment]
        )
        events: list[dict[str, Any]] = []
        for row in rows:
            dto = await self._cluster_to_dto(
                tenant_id,
                row,
                ack=None,
                user_id=None,
                include_stale=True,
            )
            if dto is None:
                continue
            events.append(
                {
                    "id": dto["id"],
                    "security_id": dto["security_id"],
                    "security_ticker": dto["security_ticker"],
                    "event_type": dto["event_type"],
                    "event_date": dto["event_date"],
                    "headline": dto["headline"],
                    "score": dto["score"],
                }
            )
        return {"events": events, "total": len(events)}

    # ── Public: acknowledgement ─────────────────────────────────────

    async def set_ack(
        self,
        tenant_id: str,
        user_id: str,
        cluster_id: str,
        acknowledged: bool,
    ) -> bool:
        """Set (or clear) the per-user ack for *cluster_id*.

        Idempotent: re-acking or un-acking the same cluster is a no-op.
        A cross-tenant cluster id returns False (never leaks existence).
        Adding a new source link to a cluster later never resets an
        existing ack (the ack row is keyed on cluster, not on items).
        """
        cluster = await self._uow.relevance_clusters.get(cluster_id)
        if cluster is None or str(cluster.tenant_id) != str(tenant_id):
            return False

        stmt = select(RelevanceAck).where(
            RelevanceAck.tenant_id == tenant_id,  # type: ignore[attr-defined]
            RelevanceAck.user_id == user_id,  # type: ignore[attr-defined]
            RelevanceAck.cluster_id == cluster_id,  # type: ignore[attr-defined]
        )
        ack = (await self._uow.session.execute(stmt)).scalar_one_or_none()  # type: ignore[assignment]
        if ack is None:
            ack = RelevanceAck(
                tenant_id=tenant_id,
                user_id=user_id,
                cluster_id=cluster_id,
                acknowledged=acknowledged,
                acknowledged_at=datetime.now(UTC),
            )
            await self._uow.relevance_acks.add(ack)
        elif ack.acknowledged != acknowledged:
            ack.acknowledged = acknowledged
            ack.acknowledged_at = datetime.now(UTC)
            await self._uow.relevance_acks.update(ack)
        return True

    # ── Public: corrections ─────────────────────────────────────────

    async def correct(
        self,
        tenant_id: str,
        user_id: str,
        item_id: str,
        *,
        security_id: str | None = None,
        action: str = CORRECTION_DISMISS,
        reason: str | None = None,
    ) -> bool:
        """File a per-user false-positive correction for *item_id*.

        The correction suppresses the (item, security) match in the
        correcting user's feed and records feedback for the future
        matcher.  It never deletes the underlying observation and never
        affects other tenants.  Idempotent per (tenant, user, item).
        """
        item = await self._uow.market_intelligence_items.get(item_id)
        if item is None or str(item.tenant_id) != str(tenant_id):
            return False
        # Sanitise free-form user input before persistence.
        from finance_sync.utils.redaction import redact_text

        stmt = select(RelevanceCorrection).where(
            RelevanceCorrection.tenant_id == tenant_id,  # type: ignore[attr-defined]
            RelevanceCorrection.user_id == user_id,  # type: ignore[attr-defined]
            RelevanceCorrection.item_id == item_id,  # type: ignore[attr-defined]
        )
        existing = (await self._uow.session.execute(stmt)).scalar_one_or_none()  # type: ignore[assignment]
        if existing is None:
            await self._uow.relevance_corrections.add(
                RelevanceCorrection(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    item_id=item_id,
                    security_id=security_id,
                    action=action,
                    reason=redact_text(reason)[:2000] if reason else None,
                )
            )
        return True

    # ── Public: notification preferences ────────────────────────────

    async def get_notification_preference(
        self, tenant_id: str, user_id: str
    ) -> dict[str, Any]:
        """Return the user's opt-in notification settings."""
        stmt = select(RelevanceNotificationPreference).where(
            RelevanceNotificationPreference.tenant_id == tenant_id,  # type: ignore[attr-defined]
            RelevanceNotificationPreference.user_id == user_id,  # type: ignore[attr-defined]
        )
        pref = (await self._uow.session.execute(stmt)).scalar_one_or_none()  # type: ignore[assignment]
        if pref is None:
            return {
                "enabled": False,
                "lockscreen_safe": True,
                "event_types": None,
            }
        return {
            "enabled": pref.enabled,
            "lockscreen_safe": pref.lockscreen_safe,
            "event_types": pref.event_types,
        }

    async def set_notification_preference(
        self,
        tenant_id: str,
        user_id: str,
        *,
        enabled: bool | None = None,
        lockscreen_safe: bool | None = None,
        event_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create/update the user's opt-in notification settings."""
        stmt = select(RelevanceNotificationPreference).where(
            RelevanceNotificationPreference.tenant_id == tenant_id,  # type: ignore[attr-defined]
            RelevanceNotificationPreference.user_id == user_id,  # type: ignore[attr-defined]
        )
        pref = (await self._uow.session.execute(stmt)).scalar_one_or_none()  # type: ignore[assignment]
        if pref is None:
            pref = RelevanceNotificationPreference(
                tenant_id=tenant_id,
                user_id=user_id,
                enabled=bool(enabled),
                lockscreen_safe=(
                    True if lockscreen_safe is None else lockscreen_safe
                ),
                event_types=event_types,
            )
            await self._uow.relevance_notification_preferences.add(pref)
        else:
            if enabled is not None:
                pref.enabled = enabled
            if lockscreen_safe is not None:
                pref.lockscreen_safe = lockscreen_safe
            if event_types is not None:
                pref.event_types = event_types
            await self._uow.relevance_notification_preferences.update(pref)
        return {
            "enabled": pref.enabled,
            "lockscreen_safe": pref.lockscreen_safe,
            "event_types": pref.event_types,
        }

    # ── Public: notifications (opt-in, dedupe, lockscreen-safe) ─────

    async def notify_eligible(
        self,
        tenant_id: str,
        user_id: str,
        cluster_id: str,
    ) -> dict[str, Any]:
        """Send (mock) lockscreen-safe notifications for eligible clusters.

        Only fires when the user opted in, dedupes per
        (user, cluster, event_type), and the payload never carries
        position sizes or financial values.
        """
        pref = await self.get_notification_preference(tenant_id, user_id)
        if not pref["enabled"]:
            return {"sent": 0, "skipped": "disabled"}

        cluster = await self._uow.relevance_clusters.get(cluster_id)
        if cluster is None or str(cluster.tenant_id) != str(tenant_id):
            return {"sent": 0, "skipped": "not_found"}

        event_types = pref["event_types"]
        if event_types and cluster.event_type not in event_types:
            return {"sent": 0, "skipped": "event_type_not_allowed"}

        # Dedupe per (user, cluster, event_type).
        stmt = select(RelevanceNotificationLog).where(
            RelevanceNotificationLog.tenant_id == tenant_id,  # type: ignore[attr-defined]
            RelevanceNotificationLog.user_id == user_id,  # type: ignore[attr-defined]
            RelevanceNotificationLog.cluster_id == cluster_id,  # type: ignore[attr-defined]
            RelevanceNotificationLog.event_type == cluster.event_type,  # type: ignore[attr-defined]
        )
        if (
            await self._uow.session.execute(stmt)
        ).scalar_one_or_none() is not None:  # type: ignore[union-attr]
            return {"sent": 0, "skipped": "already_notified"}

        # Lockscreen-safe payload: headline + event type + date only.
        payload: dict[str, Any] = {
            "type": "holding_relevance",
            "event_type": cluster.event_type,
            "headline": cluster.headline[:200],
            "event_date": (
                cluster.event_date.isoformat() if cluster.event_date else None
            ),
            "source_url": cluster.best_source_url,
        }
        if not pref["lockscreen_safe"]:
            # Explicitly allowed to show position context — still never
            # raw financial values unless the user asked for them.
            payload["include_value"] = False

        await self._uow.relevance_notification_logs.add(
            RelevanceNotificationLog(
                tenant_id=tenant_id,
                user_id=user_id,
                cluster_id=cluster_id,
                event_type=cluster.event_type,
                sent_at=datetime.now(UTC),
                payload=payload,
            )
        )
        logger.info(
            "holding_relevance_notification",
            tenant_id=tenant_id,
            user_id=user_id,
            cluster_id=cluster_id,
            event_type=cluster.event_type,
            lockscreen_safe=pref["lockscreen_safe"],
        )
        return {"sent": 1, "skipped": None, "payload": payload}

    # ── Internal: holdings ──────────────────────────────────────────

    async def _current_holdings(self, tenant_id: str) -> list[Holding]:
        """Return the latest snapshot per (account, security) with qty > 0."""
        latest_subq = (
            select(
                Holding.account_id,
                Holding.security_id,
                func.max(Holding.observed_at).label("latest_ts"),
            )
            .where(Holding.tenant_id == tenant_id)  # type: ignore[attr-defined]
            .group_by(Holding.account_id, Holding.security_id)  # type: ignore[attr-defined]
        ).subquery()
        stmt = (
            select(Holding)
            .join(
                latest_subq,
                and_(
                    Holding.account_id == latest_subq.c.account_id,
                    Holding.security_id == latest_subq.c.security_id,
                    Holding.observed_at == latest_subq.c.latest_ts,
                ),
            )
            .where(
                Holding.tenant_id == tenant_id,  # type: ignore[attr-defined]
                Holding.quantity > 0,  # type: ignore[attr-defined]
            )
        )
        return list(
            (await self._uow.session.execute(stmt)).scalars().all()  # type: ignore[assignment]
        )

    async def _cash_account_ids(self, tenant_id: str) -> set[str]:
        """Return the tenant's cash-type account ids (savings/checking/cash).

        Cash accounts have no ``Holding`` rows, so they cannot be
        discovered through holdings.  They only matter for interest /
        currency events (bunq cash portfolios).
        """
        from finance_sync.models.enums import AccountType

        cash_types = {
            AccountType.SAVINGS.value,
            AccountType.CHECKING.value,
            AccountType.CASH.value,
        }
        stmt = select(Account.id).where(
            Account.tenant_id == tenant_id,  # type: ignore[attr-defined]
            Account.account_type.in_(cash_types),  # type: ignore[attr-defined]
            Account.is_active.is_(True),  # type: ignore[attr-defined]
        )
        rows = (await self._uow.session.execute(stmt)).scalars().all()
        return {str(r) for r in rows}

    async def _recently_sold_securities(
        self, tenant_id: str, now: datetime
    ) -> set[str]:
        """Return securities whose latest snapshot shows qty == 0."""
        # Securities that ever had a holding but no current position,
        # with their latest observed_at within the recently-sold window.
        latest_per_sec = (
            select(
                Holding.security_id,
                func.max(Holding.observed_at).label("latest_ts"),
            )
            .where(Holding.tenant_id == tenant_id)  # type: ignore[attr-defined]
            .group_by(Holding.security_id)  # type: ignore[attr-defined]
        ).subquery()
        stmt = (
            select(Holding)
            .join(
                latest_per_sec,
                and_(
                    Holding.security_id == latest_per_sec.c.security_id,
                    Holding.observed_at == latest_per_sec.c.latest_ts,
                ),
            )
            .where(
                Holding.tenant_id == tenant_id,  # type: ignore[attr-defined]
                Holding.quantity <= 0,  # type: ignore[attr-defined]
                Holding.observed_at >= now - RECENTLY_SOLD_WINDOW,  # type: ignore[attr-defined]
            )
        )
        rows = list(
            (await self._uow.session.execute(stmt)).scalars().all()  # type: ignore[assignment]
        )
        return {str(h.security_id) for h in rows}

    async def _holding_weight(
        self, tenant_id: str, security_id: str
    ) -> float | None:
        """Return the normalised 0..1 weight of *security_id* in the tenant.

        Weight = latest market value of the security divided by the
        tenant's total latest market value (across current holdings).
        Returns None when no market values are available.
        """
        holdings = await self._current_holdings(tenant_id)
        if not holdings:
            return None
        values: dict[str, float] = {}
        for h in holdings:
            mv = h.market_value
            if mv is None and h.price is not None:
                mv = h.quantity * h.price
            if mv is not None:
                values[str(h.security_id)] = values.get(
                    str(h.security_id), 0.0
                ) + float(mv)
        total = sum(values.values())
        if total <= 0:
            return None
        return min(1.0, values.get(security_id, 0.0) / total)

    async def _match_cash_events(
        self,
        tenant_id: str,
        account_ids: set[str],
        items: Sequence[MarketIntelligenceItem],
        now: datetime,
    ) -> int:
        """Match interest/currency items to cash accounts (content-relevant)."""
        del now  # unused: content-relevance is decided by event type only
        matched = 0
        for item in items:
            event_type = _event_type_for(item)
            if event_type not in (EVENT_TYPE_INTEREST, EVENT_TYPE_CURRENCY):
                continue
            if item.security_id is not None:
                continue
            # Only when the item is content-relevant to cash (interest /
            # currency / rate events).
            for account_id in account_ids:
                await self._upsert_relevance(
                    tenant_id,
                    item,
                    security_id=None,  # cash events have no canonical security
                    account_id=account_id,
                    match_reason=MATCH_REASON_CURRENCY_INTEREST,
                    confidence=0.6,
                    holding_status=HOLDING_STATUS_CURRENT,
                    holding_weight=None,
                )
                matched += 1
        return matched

    # ── Internal: relevance rows ────────────────────────────────────

    async def _upsert_relevance(
        self,
        tenant_id: str,
        item: MarketIntelligenceItem,
        security_id: str | None,
        *,
        account_id: str | None,
        match_reason: str,
        confidence: float,
        holding_status: str,
        holding_weight: float | None,
    ) -> HoldingRelevanceItem:
        """Insert (or no-op on) one relevance row for (item, security)."""
        stmt = select(HoldingRelevanceItem).where(
            HoldingRelevanceItem.tenant_id == tenant_id,  # type: ignore[attr-defined]
            HoldingRelevanceItem.item_id == item.id,  # type: ignore[attr-defined]
            HoldingRelevanceItem.security_id == security_id,  # type: ignore[attr-defined]
        )
        existing = (await self._uow.session.execute(stmt)).scalar_one_or_none()  # type: ignore[assignment]
        if existing is not None:
            return existing

        event_date = _event_date_for(item)
        row = HoldingRelevanceItem(
            tenant_id=tenant_id,
            item_id=str(item.id),
            security_id=security_id,
            account_id=account_id,
            match_reason=match_reason,
            confidence=confidence,
            holding_status=holding_status,
            holding_weight=holding_weight,
            event_date=event_date,
        )
        return await self._uow.holding_relevance_items.add(row)

    # ── Internal: clustering ────────────────────────────────────────

    async def _recluster(self, tenant_id: str) -> int:
        """(Re)build clusters from the tenant's relevance rows.

        Deterministic: same relevance rows → same clusters (unique on
        story_key makes this idempotent).
        """
        stmt = (
            select(HoldingRelevanceItem)
            .where(HoldingRelevanceItem.tenant_id == tenant_id)  # type: ignore[attr-defined]
            .order_by(
                HoldingRelevanceItem.event_date.asc(),  # type: ignore[attr-defined]
                HoldingRelevanceItem.created_at.asc(),  # type: ignore[attr-defined]
            )
        )
        rows = list(
            (await self._uow.session.execute(stmt)).scalars().all()  # type: ignore[assignment]
        )

        # Group by story key, keeping the best item per cluster.
        grouped: dict[str, list[HoldingRelevanceItem]] = {}
        for row in rows:
            item = await self._uow.market_intelligence_items.get(row.item_id)
            if item is None:
                continue
            event_type = _event_type_for(item)
            event_date = row.event_date
            key = _story_key(row.security_id, event_type, event_date)
            grouped.setdefault(key, []).append(row)
        created = 0
        for key, group in grouped.items():
            # Best headline: the item with the highest reliability.
            best_item: MarketIntelligenceItem | None = None
            best_rel = -1.0
            for row in group:
                item = await self._uow.market_intelligence_items.get(
                    row.item_id
                )
                if item is None:
                    continue
                rel = _reliability(item.provider)
                if rel > best_rel:
                    best_rel = rel
                    best_item = item

            stmt = select(RelevanceCluster).where(
                RelevanceCluster.tenant_id == tenant_id,  # type: ignore[attr-defined]
                RelevanceCluster.story_key == key,  # type: ignore[attr-defined]
            )
            cluster = (
                await self._uow.session.execute(stmt)
            ).scalar_one_or_none()  # type: ignore[assignment]

            security_id = group[0].security_id
            event_type = (
                _event_type_for(best_item) if best_item is not None else "news"
            )
            event_date = group[0].event_date
            score = self._cluster_score(group, best_item)

            source_count = 0
            best_url: str | None = None
            best_url_rel = -1.0
            source_urls: list[str] = []
            for row in group:
                item = await self._uow.market_intelligence_items.get(
                    row.item_id
                )
                if item is None:
                    continue
                if item.canonical_url:
                    source_count += 1
                    if item.canonical_url not in source_urls:
                        source_urls.append(item.canonical_url)
                    rel = _reliability(item.provider)
                    if rel > best_url_rel:
                        best_url_rel = rel
                        best_url = item.canonical_url

            headline = (
                (best_item.headline or "Untitled") if best_item else "Untitled"
            )

            if cluster is None:
                cluster = RelevanceCluster(
                    tenant_id=tenant_id,
                    story_key=key,
                    security_id=security_id,
                    headline=headline,
                    event_type=event_type,
                    event_date=event_date,
                    source_count=source_count,
                    best_source_url=best_url,
                    score=score,
                )
                await self._uow.relevance_clusters.add(cluster)
                created += 1
            else:
                cluster.headline = (
                    headline if best_item is not None else cluster.headline
                )
                cluster.event_type = event_type
                cluster.event_date = event_date
                cluster.source_count = source_count
                cluster.best_source_url = best_url
                cluster.score = score
                await self._uow.relevance_clusters.update(cluster)

            # Membership edges (position = deterministic ordering).
            await self._sync_cluster_items(tenant_id, cluster.id, group)

        return created

    async def _sync_cluster_items(
        self,
        tenant_id: str,
        cluster_id: str,
        group: Sequence[HoldingRelevanceItem],
    ) -> None:
        """Idempotently attach the group's items to the cluster."""
        existing_stmt = select(RelevanceClusterItem).where(
            RelevanceClusterItem.cluster_id == cluster_id  # type: ignore[attr-defined]
        )
        existing = {
            str(e.item_id)
            for e in (await self._uow.session.execute(existing_stmt))
            .scalars()
            .all()  # type: ignore[assignment]
        }
        for position, row in enumerate(group):
            if str(row.item_id) in existing:
                continue
            await self._uow.relevance_cluster_items.add(
                RelevanceClusterItem(
                    tenant_id=tenant_id,
                    cluster_id=cluster_id,
                    item_id=str(row.item_id),
                    position=position,
                )
            )

    def _cluster_score(
        self,
        group: Sequence[HoldingRelevanceItem],
        best_item: MarketIntelligenceItem | None,
    ) -> float:
        """Deterministic ranking score in 0..1.

        Score = weight x event-proximity x recency x reliability, where
        each factor is normalised to 0..1.  Same input always yields the
        same order (no randomness, no wall-clock dependence beyond the
        item dates themselves).
        """
        now = datetime.now(UTC)

        weight = max((r.holding_weight or 0.0) for r in group) if group else 0.0

        event_date = group[0].event_date if group else None
        if event_date is None:
            proximity = 0.5
        else:
            delta = abs((now - _as_utc(event_date)).total_seconds())  # type: ignore[operator]
            # 7-day half-life: events 7 days out score ~0.5, 14 days ~0.25.
            proximity = max(0.0, 1.0 / (1.0 + delta / (7 * 86400)))

        published = best_item.published_at if best_item else now
        age_s = max(0.0, (now - _as_utc(published)).total_seconds())  # type: ignore[operator]
        # 3-day half-life for recency.
        recency = max(0.0, 1.0 / (1.0 + age_s / (3 * 86400)))

        reliability = _reliability(best_item.provider) if best_item else 0.5
        event_weight = _EVENT_TYPE_WEIGHT.get(
            _event_type_for(best_item) if best_item else "news", 0.5
        )

        score = weight * proximity * recency * reliability * event_weight
        return round(min(1.0, max(0.0, score)), 6)

    # ── Internal: read helpers ──────────────────────────────────────

    async def _ack_map(
        self,
        tenant_id: str,
        user_id: str,
        cluster_ids: list[str],
    ) -> dict[str, RelevanceAck]:
        if not cluster_ids:
            return {}
        stmt = select(RelevanceAck).where(
            RelevanceAck.tenant_id == tenant_id,  # type: ignore[attr-defined]
            RelevanceAck.user_id == user_id,  # type: ignore[attr-defined]
            RelevanceAck.cluster_id.in_(cluster_ids),  # type: ignore[attr-defined]
        )
        rows = list(
            (await self._uow.session.execute(stmt)).scalars().all()  # type: ignore[assignment]
        )
        return {str(r.cluster_id): r for r in rows}

    async def _filter_by_account(
        self,
        tenant_id: str,
        account_id: str,
        clusters: Sequence[RelevanceCluster],
    ) -> list[RelevanceCluster]:
        """Keep only clusters whose items touch *account_id*."""
        if not clusters:
            return []
        cluster_ids = [str(c.id) for c in clusters]
        stmt = select(RelevanceClusterItem).where(
            RelevanceClusterItem.tenant_id == tenant_id,  # type: ignore[attr-defined]
            RelevanceClusterItem.cluster_id.in_(cluster_ids),  # type: ignore[attr-defined]
        )
        edges = list(
            (await self._uow.session.execute(stmt)).scalars().all()  # type: ignore[assignment]
        )
        item_ids = {str(e.item_id) for e in edges}
        if not item_ids:
            return []
        rel_stmt = select(HoldingRelevanceItem).where(
            HoldingRelevanceItem.tenant_id == tenant_id,  # type: ignore[attr-defined]
            HoldingRelevanceItem.item_id.in_(item_ids),  # type: ignore[attr-defined]
            HoldingRelevanceItem.account_id == account_id,  # type: ignore[attr-defined]
        )
        matched_items = {
            str(r.item_id)
            for r in (await self._uow.session.execute(rel_stmt)).scalars().all()  # type: ignore[assignment]
        }
        keep_ids = {
            str(e.cluster_id) for e in edges if str(e.item_id) in matched_items
        }
        return [c for c in clusters if str(c.id) in keep_ids]

    async def _cluster_to_dto(
        self,
        tenant_id: str,
        cluster: RelevanceCluster,
        *,
        ack: RelevanceAck | None,
        user_id: str | None,
        include_stale: bool,
    ) -> dict[str, Any] | None:
        """Project one cluster + its source items into a feed DTO.

        Always carries source URLs, published/fetched timestamps and a
        freshness value.  Headlines are served as data (never
        evaluated); consumers escape before rendering.
        """
        # Source items via membership edges.
        stmt = (
            select(MarketIntelligenceItem)
            .join(
                RelevanceClusterItem,
                RelevanceClusterItem.item_id == MarketIntelligenceItem.id,  # type: ignore[attr-defined]
            )
            .where(
                RelevanceClusterItem.cluster_id == cluster.id,  # type: ignore[attr-defined]
                RelevanceClusterItem.tenant_id == tenant_id,  # type: ignore[attr-defined]
            )
            .order_by(RelevanceClusterItem.position.asc())  # type: ignore[attr-defined]
        )
        items = list(
            (await self._uow.session.execute(stmt)).scalars().all()  # type: ignore[assignment]
        )
        if not include_stale:
            now = datetime.now(UTC)
            items = [i for i in items if _is_fresh(i, now)]
            if not items:
                return None

        # Per-user corrections suppress matched items.
        if user_id:
            items = await self._apply_corrections(
                tenant_id, user_id, cluster.id, items
            )
            if not items:
                return None

        security_ticker: str | None = None
        security_name: str | None = None
        if cluster.security_id:
            sec = await self._uow.securities.get(cluster.security_id)
            if sec is not None:
                security_ticker = sec.ticker
                security_name = sec.name

        sources: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        for item in items:
            sources.append(  # noqa: PERF401 — dict literal readability
                {
                    "item_id": str(item.id),
                    "provider": item.provider,
                    "source_id": item.source_id,
                    "url": item.canonical_url,
                    "headline": item.headline,
                    "published_at": item.published_at,
                    "fetched_at": item.fetched_at,
                    "freshness": (
                        FRESHNESS_FRESH
                        if _is_fresh(item, now)
                        else FRESHNESS_STALE
                    ),
                    "license_class": item.license_class,
                }
            )

        acknowledged = False
        if ack is not None:
            acknowledged = ack.acknowledged
        elif user_id is not None:
            # No ack row yet → unread for this user.
            acknowledged = False

        return {
            "id": str(cluster.id),
            "security_id": cluster.security_id,
            "security_ticker": security_ticker,
            "security_name": security_name,
            "event_type": cluster.event_type,
            "event_date": cluster.event_date,
            "headline": cluster.headline,
            "score": cluster.score,
            "source_count": cluster.source_count,
            "best_source_url": cluster.best_source_url,
            "acknowledged": acknowledged,
            "sources": sources,
        }

    async def _apply_corrections(
        self,
        tenant_id: str,
        user_id: str,
        cluster_id: str,
        items: Sequence[MarketIntelligenceItem],
    ) -> list[MarketIntelligenceItem]:
        """Remove items the user corrected (suppression, never delete)."""
        # corrections are item-scoped; the cluster id is not needed
        del cluster_id
        if not items:
            return []
        stmt = select(RelevanceCorrection).where(
            RelevanceCorrection.tenant_id == tenant_id,  # type: ignore[attr-defined]
            RelevanceCorrection.user_id == user_id,  # type: ignore[attr-defined]
            RelevanceCorrection.item_id.in_(
                [  # type: ignore[attr-defined]
                    str(i.id) for i in items
                ]
            ),
        )
        corrected = {
            str(c.item_id)
            for c in (await self._uow.session.execute(stmt)).scalars().all()  # type: ignore[assignment]
        }
        if not corrected:
            return list(items)
        return [i for i in items if str(i.id) not in corrected]
