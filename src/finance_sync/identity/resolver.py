"""IdentityResolutionService — Security identity resolution pipeline.

Four-stage resolution:
  1. Exact ISIN match against canonical securities
  2. FIGI / Symbol match via OpenBB lookup
  3. Fuzzy name match against known securities (similarity scoring)
  4. Manual queue — unresolved securities stored for human review

Also includes cleansing rules (currency normalisation, whitespace trimming,
name standardisation) and audit logging of every decision.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from finance_sync.enrichment.models import ResolvedSecurity
from finance_sync.enrichment.security_resolver import SecurityResolver
from finance_sync.identity import ResolutionPipelineResult
from finance_sync.models.resolution_audit_log import ResolutionAuditLog
from finance_sync.models.unresolved_security import UnresolvedSecurity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from finance_sync.db.uow import UnitOfWork
    from finance_sync.enrichment.gateway import EnrichmentGateway
    from finance_sync.models.security import Security


# ── Cleansing rules ─────────────────────────────────────────────────────


def cleanse_currency_code(raw: str | None) -> str | None:
    """Normalise a currency code to uppercase ISO-4217."""
    if raw is None:
        return None
    cleaned = raw.strip().upper()
    if len(cleaned) != 3 or not cleaned.isalpha():
        return None  # invalid, don't guess
    return cleaned


def cleanse_name(raw: str | None) -> str | None:
    """Normalise an instrument name: trim, collapse whitespace,
    strip common suffixes."""
    if raw is None:
        return None
    cleaned = raw.strip()
    cleaned = re.sub(r"\s+", " ", cleaned)  # collapse multiple spaces
    # Strip common noise suffixes
    suffixes = [
        " - Depository Receipt",
        " - Registered Shares",
        " - Common Stock",
    ]
    for suffix in suffixes:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned or None


def cleanse_ticker(raw: str | None) -> str | None:
    """Normalise a ticker symbol."""
    if raw is None:
        return None
    cleaned = raw.strip().upper()
    return cleaned or None


def cleanse_metadata(raw: dict[str, Any] | str | None) -> str | None:
    """Normalise provider metadata to a JSON string."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return json.dumps(raw, sort_keys=True, default=str)
    if isinstance(raw, str):
        return raw
    return str(raw)


def apply_all_cleansing(instrument: dict[str, Any]) -> dict[str, Any]:
    """Apply all cleansing rules to a raw instrument dict.

    Returns a new dict with cleansed values (originals preserved
    under original_* keys for audit).
    """
    result = dict(instrument)

    for field in ("name", "description"):
        result[f"original_{field}"] = result.get(field)
    result["original_currency_code"] = result.get("currency_code")
    result["original_ticker"] = result.get("ticker") or result.get("symbol")

    name_val = result.get("name") or result.get("description")
    result["name"] = cleanse_name(name_val)
    result["currency_code"] = cleanse_currency_code(result.get("currency_code"))
    ticker = result.get("ticker") or result.get("symbol")
    result["ticker"] = cleanse_ticker(ticker)
    result["metadata"] = cleanse_metadata(
        result.get("provider_metadata") or result.get("metadata")
    )

    return result


# ── Similarity scoring helpers ──────────────────────────────────────────


def _token_sort_key(name: str) -> str:  # type: ignore[reportUnusedFunction]
    """Normalise a name for comparison: lowercase, sort tokens."""
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    tokens.sort()
    return " ".join(tokens)


def _name_similarity(a: str | None, b: str | None) -> float:
    """Compute a simple name similarity score between 0.0 and 1.0.

    Uses token-sort + Jaccard-like overlap on token sets.
    """
    if not a or not b:
        return 0.0
    a_tokens = set(re.findall(r"[a-z0-9]+", a.lower()))
    b_tokens = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not a_tokens or not b_tokens:
        return 0.0
    intersection = a_tokens & b_tokens
    union = a_tokens | b_tokens
    return len(intersection) / len(union)


# ── Service ─────────────────────────────────────────────────────────────

FUZZY_MATCH_THRESHOLD = 0.6


class IdentityResolutionService:
    """Orchestrates the 4-stage security identity resolution pipeline."""

    def __init__(
        self,
        uow: UnitOfWork,
        resolver: SecurityResolver,
        gateway: EnrichmentGateway,
    ) -> None:
        self._uow = uow
        self._resolver = resolver
        self._gateway = gateway

    # ── Public API ───────────────────────────────────────────────────────

    async def process_incoming_securities(
        self,
        provider_key: str,
        instruments: list[dict[str, Any]],
        *,
        resolver_principal: str = "system",
    ) -> ResolutionPipelineResult:
        """Run the full 4-stage resolution pipeline on incoming instruments.

        Args:
            provider_key: Connector provider identifier.
            instruments: List of raw instrument dicts from the connector.
            resolver_principal: Who/what is performing the resolution.

        Returns:
            Summary of how many were resolved at each stage.
        """
        audit_entries = 0
        resolved_auto = 0
        resolved_fuzzy = 0
        unresolved_count = 0

        for instrument in instruments:
            # Stage 0: Cleanse
            cleansed = apply_all_cleansing(instrument)

            # Stage 1: Exact ISIN match
            result = await self._stage_1_exact_isin(cleansed)
            if result is not None:
                await self._audit(result, "auto_isin", resolver_principal)
                audit_entries += 1
                resolved_auto += 1
                continue

            # Stage 2: FIGI / Symbol match via OpenBB
            result = await self._stage_2_figi_ticker(cleansed, provider_key)
            if result is not None:
                await self._audit(result, "auto_figi", resolver_principal)
                audit_entries += 1
                resolved_auto += 1
                continue

            # Stage 3: Fuzzy name match
            result = await self._stage_3_fuzzy_name(cleansed)
            if result is not None:
                await self._audit(result, "fuzzy_name", resolver_principal)
                audit_entries += 1
                resolved_fuzzy += 1
                continue

            # Stage 4: Manual queue
            await self._stage_4_enqueue(provider_key, cleansed)
            unresolved_count += 1

        return ResolutionPipelineResult(
            total_input=len(instruments),
            resolved_auto=resolved_auto,
            resolved_fuzzy=resolved_fuzzy,
            unresolved=unresolved_count,
            audit_entries=audit_entries,
        )

    # ── Stage implementations ────────────────────────────────────────────

    async def _stage_1_exact_isin(
        self,
        cleansed: dict[str, Any],
    ) -> ResolvedSecurity | None:
        """Stage 1: Exact ISIN match against local canonical securities."""
        isin = cleansed.get("isin") or cleansed.get("ISIN")
        if not isin:
            return None

        isin = isin.strip().upper()
        # Use the existing SecurityResolver for DB lookup
        result = await self._resolver.resolve_by_isin(isin)
        if isinstance(result, ResolvedSecurity):
            return result

        # Try searching listings too (same ISIN may be in SecurityListing)
        listings = await self._uow.security_listings.list(
            self._uow.security_listings.model_class.ticker == isin,  # type: ignore[attr-defined]
            limit=1,
        )
        if listings:
            listing = listings[0]
            sec = await self._uow.securities.get(listing.security_id)
            if sec is not None:
                return ResolvedSecurity(
                    security_id=sec.id,
                    isin=sec.isin,
                    figi=sec.figi,
                    ticker=sec.ticker or listing.ticker,
                    name=sec.name,
                    currency_code=sec.currency_code,
                    confidence="exact",
                    source="local_db",
                )

        return None

    async def _stage_2_figi_ticker(
        self,
        cleansed: dict[str, Any],
        _provider_key: str = "",
    ) -> ResolvedSecurity | None:
        """Stage 2: FIGI / Symbol match via OpenBB lookup.

        Reuses the existing SecurityResolver for FIGI, ticker, and
        OpenBB gateway lookups.
        """
        figi = cleansed.get("figi") or cleansed.get("FIGI")
        ticker = cleansed.get("ticker")

        # Try FIGI first (higher confidence)
        if figi:
            result = await self._resolver.resolve_by_figi(figi)
            if isinstance(result, ResolvedSecurity):
                return result

        # Try ticker via local DB
        if ticker:
            result = await self._resolver.resolve_by_ticker(ticker)
            if isinstance(result, ResolvedSecurity):
                return result

            # Try SecurityListing for ticker
            listings = await self._uow.security_listings.list(
                self._uow.security_listings.model_class.ticker == ticker,  # type: ignore[attr-defined]
                limit=1,
            )
            if listings:
                listing = listings[0]
                sec = await self._uow.securities.get(listing.security_id)
                if sec is not None:
                    return ResolvedSecurity(
                        security_id=sec.id,
                        isin=sec.isin,
                        figi=sec.figi,
                        ticker=ticker,
                        name=sec.name,
                        currency_code=sec.currency_code,
                        confidence="high",
                        source="local_db_listing",
                    )

        # Try OpenBB gateway directly for ticker
        if ticker and not self._gateway.is_degraded:
            result = await self._resolver.resolve_by_ticker(ticker)
            if isinstance(result, ResolvedSecurity):
                return result

        return None

    async def _stage_3_fuzzy_name(
        self,
        cleansed: dict[str, Any],
    ) -> ResolvedSecurity | None:
        """Stage 3: Fuzzy name match against known securities.

        Scores the incoming name against all canonical securities
        and returns the best match above threshold.
        """
        raw_name = cleansed.get("name")
        if not raw_name:
            return None

        # Get all canonical securities for scoring
        all_securities = await self._uow.securities.list()
        if not all_securities:
            return None

        cleansed_name = cleanse_name(raw_name) or raw_name
        best_score = 0.0
        best_security: Security | None = None

        for sec in all_securities:
            score = _name_similarity(cleansed_name, sec.name)
            if score > best_score:
                best_score = score
                best_security = sec

            # Also check listings for alternate names
            if sec.ticker and (
                _name_similarity(cleansed_name, sec.ticker) > best_score
            ):
                best_score = _name_similarity(cleansed_name, sec.ticker)
                best_security = sec

        if best_security is not None and best_score >= FUZZY_MATCH_THRESHOLD:
            return ResolvedSecurity(
                security_id=best_security.id,
                isin=best_security.isin,
                figi=best_security.figi,
                ticker=best_security.ticker,
                name=best_security.name,
                currency_code=best_security.currency_code,
                confidence="medium",
                source="fuzzy_name",
            )

        # Try OpenBB name search as fallback
        if not self._gateway.is_degraded:
            result = await self._resolver._resolve_via_gateway(  # type: ignore[union-attr]  # noqa: SLF001  # nosec
                identifier=cleansed_name,
                identifier_type="name",
            )
            if isinstance(result, ResolvedSecurity):
                # If OpenBB found it, create a canonical record
                await self._upsert_canonical_security(result)
                return result

        return None

    async def _stage_4_enqueue(
        self,
        provider_key: str,
        cleansed: dict[str, Any],
    ) -> UnresolvedSecurity:
        """Stage 4: Store as unresolved for the manual review queue."""
        ext_id = (
            cleansed.get("id")
            or cleansed.get("external_security_id")
            or cleansed.get("external_id")
            or (cleansed.get("ticker") or cleansed.get("isin") or "unknown")
        )
        if ext_id is not None and not isinstance(ext_id, str):
            ext_id = str(ext_id)
        else:
            ext_id = "unknown"

        existing = await self._uow.unresolved_securities.list(
            UnresolvedSecurity.provider_key == provider_key,  # type: ignore[attr-defined]
            UnresolvedSecurity.external_security_id == ext_id,  # type: ignore[attr-defined]
            limit=1,
        )
        if existing:
            # Already enqueued — update with fresh data
            record = existing[0]
            record.raw_isin = cleansed.get("isin") or record.raw_isin
            record.raw_figi = (
                cleansed.get("figi") or cleansed.get("FIGI") or record.raw_figi
            )
            record.raw_ticker = cleansed.get("ticker") or record.raw_ticker
            record.raw_name = (
                cleansed.get("name")
                or cleansed.get("description")
                or record.raw_name
            )
            record.raw_currency_code = (
                cleansed.get("currency_code") or record.raw_currency_code
            )
            raw_meta = cleansed.get("metadata") or record.raw_metadata
            record.raw_metadata = raw_meta
            await self._uow.unresolved_securities.update(record)
            return record

        record = UnresolvedSecurity(
            provider_key=provider_key,
            external_security_id=ext_id,
            raw_isin=cleansed.get("isin"),
            raw_figi=cleansed.get("figi"),
            raw_ticker=cleansed.get("ticker"),
            raw_name=cleansed.get("name") or cleansed.get("description"),
            raw_currency_code=cleansed.get("currency_code"),
            raw_metadata=cleansed.get("metadata"),
        )
        await self._uow.unresolved_securities.add(record)
        return record

    # ── Manual resolution ────────────────────────────────────────────────

    async def manually_resolve(
        self,
        unresolved_id: str,
        target_security_id: str,
        *,
        resolver_principal: str = "system",
        resolution_notes: str | None = None,
    ) -> ResolutionAuditLog | None:
        """Manually link an unresolved security to a canonical record."""
        # Fetch the unresolved record
        unresolved = await self._uow.unresolved_securities.get(unresolved_id)
        if unresolved is None:
            return None

        # Verify the target security exists
        target = await self._uow.securities.get(target_security_id)
        if target is None:
            return None

        # Update the unresolved record
        unresolved.resolved_security_id = target_security_id
        unresolved.resolution_method = "manual"
        unresolved.resolution_notes = resolution_notes
        await self._uow.unresolved_securities.update(unresolved)

        # Create audit log entry
        audit = ResolutionAuditLog(
            unresolved_security_id=unresolved.id,
            source_security_id=(
                unresolved.raw_isin
                or unresolved.raw_ticker
                or unresolved.external_security_id
            ),
            target_security_id=target_security_id,
            resolution_method="manual",
            confidence="high",
            resolver_principal=resolver_principal,
            resolved_at=datetime.now(UTC),
            resolution_detail=(
                resolution_notes or "Manual resolution by operator"
            ),
        )
        await self._uow.resolution_audit_log.add(audit)

        # Trigger background enrichment for the newly resolved security
        await self._background_enrich(target_security_id, unresolved)

        return audit

    async def map_and_resolve(
        self,
        provider_key: str,
        external_security_id: str,
        target_security_id: str,
        *,
        resolver_principal: str = "system",
        resolution_notes: str | None = None,
    ) -> ResolutionAuditLog | None:
        """Map a specific incoming security (by provider key + ext ID)
        to a canonical record without needing the unresolved record ID.

        Creates an UnresolvedSecurity record if one doesn't exist yet.
        """
        # Find or create an unresolved record
        existing = await self._uow.unresolved_securities.list(
            UnresolvedSecurity.provider_key == provider_key,  # type: ignore[attr-defined]
            UnresolvedSecurity.external_security_id == external_security_id,  # type: ignore[attr-defined]
            limit=1,
        )

        if existing:
            unresolved = existing[0]
        else:
            unresolved = UnresolvedSecurity(
                provider_key=provider_key,
                external_security_id=external_security_id,
            )
            await self._uow.unresolved_securities.add(unresolved)

        # Verify target exists
        target = await self._uow.securities.get(target_security_id)
        if target is None:
            return None

        unresolved.resolved_security_id = target_security_id
        unresolved.resolution_method = "manual"
        unresolved.resolution_notes = resolution_notes
        await self._uow.unresolved_securities.update(unresolved)

        # Create audit log
        audit = ResolutionAuditLog(
            unresolved_security_id=unresolved.id,
            source_security_id=external_security_id,
            target_security_id=target_security_id,
            resolution_method="manual",
            confidence="high",
            resolver_principal=resolver_principal,
            resolved_at=datetime.now(UTC),
            resolution_detail=(
                resolution_notes or f"Direct mapping by {resolver_principal}"
            ),
        )
        await self._uow.resolution_audit_log.add(audit)

        await self._background_enrich(target_security_id, unresolved)

        return audit

    # ── Background enrichment ────────────────────────────────────────────

    async def _background_enrich(
        self,
        security_id: str,
        unresolved: UnresolvedSecurity,
    ) -> None:
        """When a security is newly resolved, auto-fetch its price history.

        This uses the existing EnrichmentGateway to fetch historical
        prices and store them via PriceStore.
        """
        if self._gateway.is_degraded:
            return

        try:
            # Use the ticker or ISIN for price fetching
            identifier = unresolved.raw_ticker or unresolved.raw_isin
            if not identifier:
                return

            await self._gateway.get_historical_prices(
                security_id=security_id,
                identifier=identifier,
                identifier_type="ticker" if unresolved.raw_ticker else "isin",
                interval="1d",
                limit=365,  # ~1 year of daily data
            )

            await self._gateway.update_freshness(
                security_id=security_id,
                field="last_daily_price_fetch",
                status="resolved",
            )
        except Exception:
            # Background enrichment failure should not block the resolution
            pass

    # ── Audit helpers ────────────────────────────────────────────────────

    async def _audit(
        self,
        result: ResolvedSecurity,
        method: str,
        resolver_principal: str,
    ) -> None:
        """Record a resolution decision in the audit log."""
        audit = ResolutionAuditLog(
            target_security_id=result.security_id,
            resolution_method=method,
            confidence=result.confidence,
            resolver_principal=resolver_principal,
            resolved_at=datetime.now(UTC),
            resolution_detail=(
                f"Auto-resolved via {method}: "
                f"isin={result.isin!r} ticker={result.ticker!r} "
                f"name={result.name!r}"
            ),
        )
        await self._uow.resolution_audit_log.add(audit)

    # ── Helpers ──────────────────────────────────────────────────────────

    async def get_unresolved(
        self,
        *,
        only_unmapped: bool = True,
        provider_key: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[UnresolvedSecurity]:
        """List unresolved securities, optionally filtered."""
        filters = []
        if only_unmapped:
            filters.append(
                UnresolvedSecurity.resolved_security_id.is_(None)  # type: ignore[attr-defined]
            )
        if provider_key:
            filters.append(
                UnresolvedSecurity.provider_key == provider_key  # type: ignore[attr-defined]
            )
        return (
            await self._uow.unresolved_securities.list(
                *filters,
                order_by=UnresolvedSecurity.created_at.desc(),  # type: ignore[attr-defined]
                limit=limit,
                offset=offset,
            )
            or []
        )

    async def get_audit_log(
        self,
        *,
        target_security_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[ResolutionAuditLog]:
        """List resolution audit log entries."""
        filters = []
        if target_security_id:
            filters.append(
                ResolutionAuditLog.target_security_id == target_security_id  # type: ignore[attr-defined]
            )
        return await self._uow.resolution_audit_log.list(
            *filters,
            order_by=ResolutionAuditLog.resolved_at.desc(),  # type: ignore[attr-defined]
            limit=limit,
        )

    async def _upsert_canonical_security(
        self,
        resolved: ResolvedSecurity,
    ) -> None:
        """Create or update a canonical Security record from a resolution."""
        existing = await self._uow.securities.list(
            self._uow.securities.model_class.isin == resolved.isin,  # type: ignore[attr-defined]
            limit=1,
        )
        if existing:
            return  # already exists

        sec = self._uow.securities.model_class(
            isin=resolved.isin,
            figi=resolved.figi,
            ticker=resolved.ticker,
            name=resolved.name,
            security_type=SecurityResolver.infer_security_type(
                resolved.ticker or "", resolved.figi
            ),
            currency_code=resolved.currency_code,
        )
        await self._uow.securities.add(sec)
