"""Market-intelligence ingestion service.

Turns provider items (:class:`IntelItem`) into persisted, deduplicated,
licence-compliant observations:

1. **Licensing policy** — the item's ``license_class`` drives what may
   be stored.  Full text only for permissive classes; a short snippet
   only for classes that allow it; everything else keeps metadata,
   structured facts and a canonical link.
2. **Deduplication** — idempotent on ``(tenant_id, provider,
   source_id)`` and ``(tenant_id, content_hash)``.  Re-fetching the
   same syndicated item is a no-op; provider outages never delete
   previously valid rows.
3. **Identity resolution** — candidate identifiers are resolved
   through the existing FIGI/ISIN/ticker/listing pipeline
   (:class:`SecurityResolver`).  Ambiguous matches are flagged for
   review (``review_required``) and never silently attached to a
   holding.
4. **Provider state** — every run records timestamps, latency, quota,
   freshness and a sanitised error, so a source that is down is
   explicitly ``unavailable`` instead of silently empty.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from finance_sync.intel.enums import (
    FULL_CONTENT_LICENSE_CLASSES,
    SNIPPET_LICENSE_CLASSES,
    IntelCapability,
    IntelLicenseClass,
    IntelResolutionStatus,
)
from finance_sync.intel.exceptions import IntelLicensingError
from finance_sync.intel.identity import IntelIdentityResolution
from finance_sync.intel.licensing import (
    enforce_snippet_limit,
    infer_license_class,
)
from finance_sync.models.market_intelligence_item import (
    MarketIntelligenceItem,
)
from finance_sync.models.market_intelligence_provider_state import (
    INTEL_PROVIDER_STATUSES,
    MarketIntelligenceProviderState,
)
from finance_sync.models.market_intelligence_review_queue import (
    MarketIntelligenceReviewQueue,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from finance_sync.db.uow import UnitOfWork
    from finance_sync.enrichment.security_resolver import SecurityResolver
    from finance_sync.intel.models import IntelItem, IntelStructuredFact
    from finance_sync.intel.provider import IntelProvider

logger = structlog.get_logger(__name__)


#: Sanitised truncation for stored provider errors.
_MAX_ERROR_LEN = 500


def apply_licensing_policy(item: IntelItem) -> IntelItem:
    """Return a copy of *item* whose content honours its licence class.

    * ``body`` is kept only for permissive classes
      (``public_domain`` / ``open_license``) — otherwise dropped.
    * ``summary`` is kept only when the class allows snippets, and is
      truncated to :data:`DEFAULT_SNIPPET_MAX_CHARS` characters
      (multi-byte safe: the cap is on characters, never bytes).
    * ``headline``, structured facts, metadata and the canonical link
      are always kept.

    Raises :class:`IntelLicensingError` when an adapter explicitly
    declared a restricted ``license_class`` yet asked to store full text
    (a programming error).  When a raw ``license_text`` string drives a
    restricted effective class, the body/snippet are silently dropped —
    the raw source string is authoritative over the adapter's hint.
    """
    license_class = _effective_license_class(item)

    # Programming-error check (only when no raw license_text is
    # present): an adapter that explicitly declared a restricted class
    # AND asked for full text is broken, regardless of the effective
    # class.  When license_text IS present it is authoritative — the
    # body/snippet are silently dropped instead of raising.
    if item.license_text is None:
        if (
            item.store_full_text
            and item.body
            and (item.license_class not in FULL_CONTENT_LICENSE_CLASSES)
        ):
            msg = (
                f"provider {item.provider!r} requested full-text storage "
                f"for license class {item.license_class.value!r} which "
                "does not permit it"
            )
            raise IntelLicensingError(msg)
        if (
            item.store_summary
            and item.summary
            and (item.license_class not in SNIPPET_LICENSE_CLASSES)
        ):
            msg = (
                f"provider {item.provider!r} requested snippet storage "
                f"for license class {item.license_class.value!r} which "
                "does not permit it"
            )
            raise IntelLicensingError(msg)

    body = item.body if license_class in FULL_CONTENT_LICENSE_CLASSES else None
    summary = (
        enforce_snippet_limit(item.summary)
        if license_class in SNIPPET_LICENSE_CLASSES
        else None
    )
    return item.model_copy(
        update={
            "license_class": license_class,
            "body": body,
            "summary": summary,
            "store_full_text": item.store_full_text and body is not None,
            "store_summary": item.store_summary and summary is not None,
        }
    )


def _effective_license_class(item: IntelItem) -> IntelLicenseClass:
    """Return the licence class to enforce for *item*.

    * When the adapter surfaced a raw ``license_text`` string it is
      classified with :func:`infer_license_class` — an empty, unknown
      or deviant string (``"copyright (c) 2026"``,
      ``"CC-BY-NC-4.0"``) yields ``proprietary``, which forbids
      snippets and full text.
    * Otherwise the adapter's explicit ``license_class`` is used as-is
      (adapter authors are responsible for setting it correctly).
    """
    if item.license_text is not None:
        return infer_license_class(item.license_text)
    return item.license_class


def sanitise_provider_error(exc: BaseException | str) -> str:
    """Return a short, secret-free error message for persistence."""
    from finance_sync.utils.redaction import redact_text

    return redact_text(str(exc))[:_MAX_ERROR_LEN]


class IntelIngestionService:
    """Ingests market-intelligence items into the observation store."""

    def __init__(
        self,
        uow: UnitOfWork,
        resolver: SecurityResolver,
    ) -> None:
        self._uow = uow
        self._identity = IntelIdentityResolution(resolver)

    # ── Public API ───────────────────────────────────────────────────

    async def ingest_items(
        self,
        tenant_id: str,
        provider_key: str,
        items: Sequence[IntelItem],
        *,
        resolve_identities: bool = True,
    ) -> dict[str, int]:
        """Persist *items* for a tenant idempotently (upsert).

        Returns a summary dict: ``{"ingested": n, "updated": n,
        "duplicates": n, "review_required": n, "errors": n}``.

        Semantics (incremental, idempotent):

        * **New item** (no row for ``(tenant, provider, source_id)`` and
          no row with the same ``content_hash``) → inserted.
        * **Same provider + source_id, different content** (the source
          updated the story) → the existing row is *updated* in place
          (never duplicated).
        * **Same content_hash from a different provider/source** (a
          syndicated duplicate) → counted as ``duplicates``, no write.
        * **Re-ingest of an identical item** → ``duplicates``, no write.

        A provider outage never deletes or invalidates stored rows —
        staleness is a separate, explicit step (:meth:`mark_stale`).
        """
        summary: dict[str, int] = {
            "ingested": 0,
            "updated": 0,
            "duplicates": 0,
            "review_required": 0,
            "errors": 0,
        }

        candidates: list[dict[str, Any]] | None = None

        for item in items:
            try:
                policy_item = apply_licensing_policy(item)
            except IntelLicensingError:
                summary["errors"] += 1
                logger.error(
                    "intel_licensing_violation",
                    provider=provider_key,
                    source_id=item.source_id,
                )
                continue

            # 1) Same (tenant, provider, source_id) → the same source
            #    re-served this item.  Unchanged content is a duplicate;
            #    changed content is an in-place update.
            same_source = await self._find_by_source(
                tenant_id, provider_key, policy_item
            )
            if same_source is not None:
                if self._is_unchanged(same_source, policy_item):
                    summary["duplicates"] += 1
                else:
                    await self._update_row(same_source, policy_item)
                    summary["updated"] += 1
                continue

            # 2) Same content_hash from a different provider/source →
            #    a syndicated duplicate.  Never mutated, never counted
            #    as an update: the first provider's provenance wins.
            by_hash = await self._find_by_hash(tenant_id, policy_item)
            if by_hash is not None:
                summary["duplicates"] += 1
                continue

            resolved_security_id: str | None = None
            resolution_status = IntelResolutionStatus.UNRESOLVED
            review_required = False
            candidates = None

            if resolve_identities and policy_item.identifiers:
                (
                    resolved_security_id,
                    resolution_status,
                    review_required,
                    candidates,
                ) = await self._identity.resolve(policy_item)
                # Ambiguous matches are NEVER attached to a holding:
                # the item stays queryable without a security link
                # and lands in the review queue instead.
                if review_required:
                    resolved_security_id = None

            row = self._build_row(
                tenant_id=tenant_id,
                item=policy_item,
                security_id=resolved_security_id,
                resolution_status=resolution_status,
                review_required=review_required,
            )
            await self._uow.market_intelligence_items.add(row)
            summary["ingested"] += 1
            if review_required:
                summary["review_required"] += 1
                await self._ensure_review_entry(
                    tenant_id=tenant_id,
                    item=policy_item,
                    row=row,
                    candidates=candidates,
                )

        return summary

    async def mark_stale(
        self,
        tenant_id: str,
        provider_key: str,
        *,
        older_than: datetime,
    ) -> int:
        """Soft-flag observations that aged past the freshness bound.

        Marks every item of *provider_key* for *tenant_id* whose
        ``fetched_at`` is older than *older_than* as ``is_stale=True``
        and sets its ``stale_after`` deadline.  Stale is a **soft**
        flag: rows are never deleted or invalidated, and an outage
        alone never marks data stale — only the freshness rule does.

        Returns the number of items newly marked stale.
        """
        repo = self._uow.market_intelligence_items
        model = repo.model_class
        rows = await repo.list(
            model.tenant_id == tenant_id,  # type: ignore[attr-defined]
            model.provider == provider_key,  # type: ignore[attr-defined]
            model.is_stale.is_(False),  # type: ignore[attr-defined]
            model.fetched_at < older_than,  # type: ignore[attr-defined]
        )
        for row in rows:
            row.is_stale = True
            row.stale_after = older_than
        if rows:
            await repo.update(rows[0])  # flush pending mutations
        return len(rows)

    async def clear_stale(
        self,
        tenant_id: str,
        provider_key: str,
    ) -> int:
        """Clear the stale flag for items re-fetched by a healthy run.

        After a successful provider refresh, items whose content is
        current again (fresh ``fetched_at``) have their stale flag
        removed so a recovery is observable, not sticky.
        """
        repo = self._uow.market_intelligence_items
        model = repo.model_class
        rows = await repo.list(
            model.tenant_id == tenant_id,  # type: ignore[attr-defined]
            model.provider == provider_key,  # type: ignore[attr-defined]
            model.is_stale.is_(True),  # type: ignore[attr-defined]
        )
        for row in rows:
            row.is_stale = False
            row.stale_after = None
        if rows:
            await repo.update(rows[0])
        return len(rows)

    async def record_provider_run(
        self,
        tenant_id: str,
        provider: IntelProvider,
        *,
        capability: IntelCapability,
        status: str,
        error: str | None = None,
        latency_ms: int | None = None,
        items_ingested: int | None = None,
    ) -> None:
        """Persist the outcome of one provider run (idempotent)."""
        if status not in INTEL_PROVIDER_STATUSES:
            status = "unavailable"
        now = datetime.now(UTC)

        logger.info(
            "intel_provider_run",
            provider=provider.provider_key,
            capability=capability.value,
            status=status,
            latency_ms=latency_ms,
        )

        state = await self._get_or_create_state(
            tenant_id, provider.provider_key
        )
        state.last_run_at = now
        state.status = status
        state.latency_ms = latency_ms
        state.items_ingested = items_ingested
        if status == "ok":
            state.last_success_at = now
            state.last_error = None
            state.last_error_class = None
        else:
            state.last_error = sanitise_provider_error(error) if error else None
            state.last_error_class = (
                type(error).__name__ if error is not None else None
            )

        state.freshness_max_age_seconds = int(
            provider.freshness.max_age.total_seconds()
        )
        state.freshness_min_interval_seconds = int(
            provider.freshness.min_interval.total_seconds()
        )
        try:
            status_snapshot = await provider.status()
            state.capabilities = [c.value for c in status_snapshot.capabilities]
            state.availability = {
                c.value: a.value
                for c, a in status_snapshot.availability.items()
            }
        except Exception:
            pass
        await self._uow.market_intelligence_provider_states.update(state)

    # ── Internal helpers ─────────────────────────────────────────────

    @staticmethod
    def _is_unchanged(
        existing: MarketIntelligenceItem,
        item: IntelItem,
    ) -> bool:
        """Return True when *existing* already holds *item*'s content.

        Comparison covers the content hash plus the storable content
        fields, so a re-fetch that returned the same story (same hash,
        same snippet/body/facts) is a true duplicate while a changed
        story is an update.
        """
        if existing.content_hash != item.content_hash:
            return False
        return (
            existing.headline == item.headline
            and existing.summary == item.summary
            and existing.body == item.body
            and existing.canonical_url == item.canonical_url
            and (existing.facts or None)
            == ([_fact_to_dict(f) for f in item.facts] or None)
        )

    async def _update_row(
        self,
        existing: MarketIntelligenceItem,
        item: IntelItem,
    ) -> None:
        """Refresh an existing row in place with *item*'s new content.

        Keeps the row id and the original ``published_at``/``source_id``
        stable (dedupe identity), refreshes the fetched-at timestamp,
        the content hash and the storable content fields, and clears a
        stale flag — the source served the item again, so it is fresh.
        """
        existing.content_hash = item.content_hash
        existing.canonical_url = item.canonical_url
        existing.headline = item.headline
        existing.summary = item.summary
        existing.body = item.body
        existing.facts = [_fact_to_dict(f) for f in item.facts] or None
        existing.provider_metadata = item.provider_metadata or None
        existing.identifiers = item.identifiers or None
        existing.fetched_at = item.fetched_at
        existing.valid_from = item.valid_from
        existing.valid_until = item.valid_until
        existing.language = item.language
        existing.license_class = item.license_class.value
        existing.license_uri = item.license_uri
        existing.is_stale = False
        existing.stale_after = None
        await self._uow.market_intelligence_items.update(existing)

    async def _find_by_source(
        self,
        tenant_id: str,
        provider_key: str,
        item: IntelItem,
    ) -> MarketIntelligenceItem | None:
        """Return the row for the same (tenant, provider, source_id)."""
        repo = self._uow.market_intelligence_items
        model = repo.model_class
        by_source = await repo.list(
            model.tenant_id == tenant_id,  # type: ignore[attr-defined]
            model.provider == provider_key,  # type: ignore[attr-defined]
            model.source_id == item.source_id,  # type: ignore[attr-defined]
            limit=1,
        )
        return by_source[0] if by_source else None

    async def _find_by_hash(
        self,
        tenant_id: str,
        item: IntelItem,
    ) -> MarketIntelligenceItem | None:
        """Return a row with the same content hash (any provider)."""
        repo = self._uow.market_intelligence_items
        model = repo.model_class
        by_hash = await repo.list(
            model.tenant_id == tenant_id,  # type: ignore[attr-defined]
            model.content_hash == item.content_hash,  # type: ignore[attr-defined]
            limit=1,
        )
        return by_hash[0] if by_hash else None

    def _build_row(
        self,
        *,
        tenant_id: str,
        item: IntelItem,
        security_id: str | None,
        resolution_status: IntelResolutionStatus,
        review_required: bool,
    ) -> MarketIntelligenceItem:
        """Build an ORM row from a (policy-cleaned) item."""
        facts: list[dict[str, Any]] = [_fact_to_dict(f) for f in item.facts]
        return MarketIntelligenceItem(
            tenant_id=tenant_id,
            provider=item.provider,
            source_id=item.source_id,
            canonical_url=item.canonical_url,
            kind=item.kind.value,
            published_at=item.published_at,
            fetched_at=item.fetched_at,
            valid_from=item.valid_from,
            valid_until=item.valid_until,
            language=item.language,
            license_class=item.license_class.value,
            license_uri=item.license_uri,
            content_hash=item.content_hash,
            headline=item.headline,
            summary=item.summary,
            body=item.body,
            facts=facts or None,
            provider_metadata=item.provider_metadata or None,
            identifiers=item.identifiers or None,
            resolution_status=resolution_status.value,
            security_id=security_id,
            review_required=review_required,
        )

    async def _ensure_review_entry(
        self,
        *,
        tenant_id: str,
        item: IntelItem,
        row: MarketIntelligenceItem,
        candidates: list[dict[str, Any]] | None,
    ) -> None:
        """Create (or keep) one review-queue entry per ambiguous item.

        Idempotent: re-ingesting the same item after an entry exists
        never creates a second entry and never overwrites a previously
        accepted resolution (``resolution_status``/``resolved_security_id``
        are left untouched).
        """
        repo = self._uow.market_intelligence_review_queue
        model = repo.model_class
        existing = await repo.list(
            model.tenant_id == tenant_id,  # type: ignore[attr-defined]
            model.item_id == row.id,  # type: ignore[attr-defined]
            limit=1,
        )
        if existing:
            return
        entry = MarketIntelligenceReviewQueue(
            tenant_id=tenant_id,
            item_id=row.id,
            provider=item.provider,
            source_id=item.source_id,
            candidate_identifiers=candidates,
            resolution_status="pending",
        )
        await repo.add(entry)

    async def _get_or_create_state(
        self,
        tenant_id: str,
        provider_key: str,
    ) -> MarketIntelligenceProviderState:
        """Return the provider-state row for (tenant, provider), creating
        it when missing."""
        repo = self._uow.market_intelligence_provider_states
        model = repo.model_class
        existing = await repo.list(
            model.tenant_id == tenant_id,  # type: ignore[attr-defined]
            model.provider == provider_key,  # type: ignore[attr-defined]
            limit=1,
        )
        if existing:
            return existing[0]
        state = MarketIntelligenceProviderState(
            tenant_id=tenant_id,
            provider=provider_key,
            status="pending",
        )
        await repo.add(state)
        return state


def _fact_to_dict(fact: IntelStructuredFact) -> dict[str, Any]:
    """Serialise a structured fact for JSONB storage."""
    return {
        "key": fact.key,
        "value": fact.value,
        "unit": fact.unit,
        "as_of": fact.as_of.isoformat() if fact.as_of else None,
        "item_source_id": fact.item_source_id,
        "item_url": fact.item_url,
    }
