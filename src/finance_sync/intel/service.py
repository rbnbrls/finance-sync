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
        self._resolver = resolver

    # ── Public API ───────────────────────────────────────────────────

    async def ingest_items(
        self,
        tenant_id: str,
        provider_key: str,
        items: Sequence[IntelItem],
        *,
        resolve_identities: bool = True,
    ) -> dict[str, int]:
        """Persist *items* for a tenant idempotently.

        Returns a summary dict: ``{"ingested": n, "updated": n,
        "duplicates": n, "review_required": n, "errors": n}``.
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

            existing = await self._find_existing(
                tenant_id, provider_key, policy_item
            )
            if existing is not None:
                summary["duplicates"] += 1
                continue

            resolved_security_id: str | None = None
            resolution_status = IntelResolutionStatus.UNRESOLVED
            review_required = False
            candidates = None

            if resolve_identities and policy_item.identifiers:
                resolution = await self._resolve_identifiers(policy_item)
                if resolution is not None:
                    (
                        resolved_security_id,
                        resolution_status,
                        review_required,
                        candidates,
                    ) = resolution
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

    async def _find_existing(
        self,
        tenant_id: str,
        provider_key: str,
        item: IntelItem,
    ) -> MarketIntelligenceItem | None:
        """Return an existing row for *item* (by provider+source_id or hash)."""
        repo = self._uow.market_intelligence_items
        model = repo.model_class
        by_source = await repo.list(
            model.tenant_id == tenant_id,  # type: ignore[attr-defined]
            model.provider == provider_key,  # type: ignore[attr-defined]
            model.source_id == item.source_id,  # type: ignore[attr-defined]
            limit=1,
        )
        if by_source:
            return by_source[0]
        by_hash = await repo.list(
            model.tenant_id == tenant_id,  # type: ignore[attr-defined]
            model.content_hash == item.content_hash,  # type: ignore[attr-defined]
            limit=1,
        )
        return by_hash[0] if by_hash else None

    async def _resolve_identifiers(
        self,
        item: IntelItem,
    ) -> tuple[str, IntelResolutionStatus, bool, list[dict[str, Any]]] | None:
        """Resolve item identifiers through the existing pipeline.

        Returns ``(security_id, resolution_status, review_required,
        candidates)`` or ``None`` when the item carries no resolvable
        identifier.  ``candidates`` is the list of matched securities
        (id + identifier + confidence) used for the review queue.
        """
        identifiers = item.identifiers or {}
        candidates_list: list[tuple[str, str]] = []
        if identifiers.get("isin"):
            candidates_list.append(("isin", identifiers["isin"]))
        if identifiers.get("figi"):
            candidates_list.append(("figi", identifiers["figi"]))
        if identifiers.get("ticker"):
            candidates_list.append(("ticker", identifiers["ticker"]))
        if not candidates_list:
            return None

        # Track whether a candidate matched but *ambiguously* (i.e. a
        # different local security was found under a second identifier).
        matched: list[tuple[str, str, ResolvedSecurityLike]] = []
        for id_type, value in candidates_list:
            if id_type == "isin":
                result = await self._resolver.resolve_by_isin(value)
            elif id_type == "figi":
                result = await self._resolver.resolve_by_figi(value)
            else:
                result = await self._resolver.resolve_by_ticker(value)
            if isinstance(result, ResolvedSecurityLike):
                matched.append((id_type, value, result))

        if not matched:
            return None

        candidates = [
            {
                "identifier_type": id_type,
                "identifier": value,
                "security_id": resolved.security_id,
                "confidence": resolved.confidence,
            }
            for id_type, value, resolved in matched
        ]

        # Ambiguity: multiple identifiers resolved to *different*
        # securities → flag for review, never auto-attach.
        distinct_ids = {resolved.security_id for _, _, resolved in matched}
        if len(distinct_ids) > 1:
            return (
                sorted(distinct_ids)[0],
                IntelResolutionStatus.AMBIGUOUS,
                True,
                candidates,
            )

        best = matched[0][2]
        confidence = (best.confidence or "").lower()
        review = confidence in {"medium", "low", "fuzzy", "inferred"}
        status = (
            IntelResolutionStatus.AMBIGUOUS
            if review
            else IntelResolutionStatus.RESOLVED
        )
        return (best.security_id, status, review, candidates)

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


# Re-export for the ambiguity check without a circular import at module
# level (enrichment.models imports enrichment services, not intel).
from finance_sync.enrichment.models import (  # noqa: E402
    ResolvedSecurity as ResolvedSecurityLike,
)
