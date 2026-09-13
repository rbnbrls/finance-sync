# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""Normalize Wealthfolio health findings into the remediation backlog.

# Dynamic provider JSON is validated at the boundary; the remainder uses
# bounded accessors and explicit runtime checks.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import func, select

from finance_sync.models import (
    ExportTarget,
    Security,
    SecurityListing,
    WealthfolioHealthCursor,
)
from finance_sync.models.remediation import DataQualityRemediationItem
from finance_sync.reconciliation.remediation.backlog import (
    BacklogRepository,
    DetectedIssue,
    deduplication_key,
)

PROVIDER_KEY = "wealthfolio"
MAX_CONTEXT_TEXT = 256
MAX_AFFECTED_ITEMS = 100


def repair_capability_is_supported(
    health_payload: object, capability_response: object
) -> bool:
    """Evaluate the sanitized A3 target/version compatibility contract."""
    if not isinstance(health_payload, dict):
        return False
    health_payload = cast("dict[str, Any]", health_payload)
    if not isinstance(health_payload.get("issues"), list):
        return False
    if not isinstance(capability_response, dict):
        return False
    health_payload = cast("dict[str, Any]", health_payload)
    capability_response = cast("dict[str, Any]", capability_response)
    version = capability_response.get("version")
    capabilities = capability_response.get("capabilities")
    if not isinstance(version, str) or not version.strip():
        return False
    if not isinstance(capabilities, dict):
        return False
    capabilities = cast("dict[str, Any]", capabilities)
    return all(
        capabilities.get(name) is True
        for name in ("quote_history_read", "quote_upsert")
    )


@dataclass(frozen=True, slots=True)
class HealthPollResult:
    """Sanitized health payload plus durable completeness metadata."""

    payload: dict[str, Any]
    complete: bool
    truncated: bool = False
    cursor_state: dict[str, Any] = field(default_factory=dict[str, Any])


def prepare_health_poll(
    payload: dict[str, Any], *, issue_limit: int
) -> HealthPollResult:
    """Bound a health response without treating omitted issues as absent."""
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return HealthPollResult(
            payload=dict(payload),
            complete=False,
            cursor_state={"reason": "malformed_issues"},
        )

    issues = cast("list[object]", issues)
    metadata: dict[str, Any] = {
        "issue_limit": max(1, issue_limit),
        "returned_issues": min(len(issues), max(1, issue_limit)),
    }
    next_cursor = payload.get("nextCursor", payload.get("next_cursor"))
    has_more = payload.get("hasMore", payload.get("has_more"))
    pagination = payload.get("pagination")
    if isinstance(pagination, dict):
        pagination = cast("dict[str, Any]", pagination)
        next_cursor = pagination.get(
            "nextCursor", pagination.get("next_cursor", next_cursor)
        )
        has_more = pagination.get(
            "hasMore", pagination.get("has_more", has_more)
        )
    if next_cursor is not None:
        metadata["next_cursor"] = _text(next_cursor, limit=128)
    if has_more is True or next_cursor:
        metadata["reason"] = "provider_pagination"
        complete = False
    elif len(issues) > issue_limit:
        metadata["reason"] = "issue_limit"
        complete = False
    else:
        metadata["reason"] = "complete"
        complete = True
    bounded = dict(payload)
    if len(issues) > issue_limit:
        bounded["issues"] = issues[: max(1, issue_limit)]
    return HealthPollResult(
        payload=bounded,
        complete=complete,
        truncated=len(issues) > issue_limit
        or bool(next_cursor)
        or has_more is True,
        cursor_state=metadata,
    )


def _text(value: object, *, limit: int = MAX_CONTEXT_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _issue_kind(issue: dict[str, Any]) -> str:
    raw = " ".join(
        _text(issue.get(key)).lower()
        for key in ("code", "category", "type", "fixAction")
    )
    # Unsafe financial findings must win over generic ``price`` wording in
    # codes such as MISSING_PURCHASE_PRICE.  They have no safe automatic
    # repair primitive and therefore remain manual-review-only.
    if "purchase" in raw or "cost_basis" in raw or "cost basis" in raw:
        return "wealthfolio_missing_purchase_price"
    if "negative" in raw and "valuation" in raw:
        return "wealthfolio_negative_valuation"
    if "incomplete" in raw and "valuation" in raw:
        return "wealthfolio_incomplete_valuation"
    if any(token in raw for token in ("transaction", "transfer")):
        return "wealthfolio_transaction_or_transfer_issue"
    if any(token in raw for token in ("quote", "price", "market_data")):
        if "histor" in raw or "gap" in raw or "missing_price" in raw:
            return "wealthfolio_historical_price_gap"
        return "wealthfolio_quote_sync_failure"
    return "wealthfolio_unsupported_issue"


def _strategy(kind: str) -> str:
    if kind == "wealthfolio_quote_sync_failure":
        return "wealthfolio_quote"
    if kind == "wealthfolio_historical_price_gap":
        return "wealthfolio_price_history"
    return "unsupported"


async def resolve_canonical_health_issues(
    session: Any, findings: list[DetectedIssue]
) -> list[DetectedIssue]:
    """Hydrate health findings from the existing canonical security tables.

    Wealthfolio asset ids are opaque and are never treated as local security
    ids.  A repair is eligible only when exactly one canonical Security can
    be proven from the explicit security id or identifier in the finding.
    """
    resolved: list[DetectedIssue] = []
    for finding in findings:
        if finding.remediation_strategy not in {
            "wealthfolio_quote",
            "wealthfolio_price_history",
        }:
            resolved.append(finding)
            continue
        context = dict(finding.context)
        security = await _find_canonical_security(session, context)
        if security is None:
            context["manual_review"] = True
            context["identity_resolution"] = "unresolved_or_ambiguous"
            resolved.append(
                replace(
                    finding, remediation_strategy="unsupported", context=context
                )
            )
            continue
        identifier, identifier_type = _canonical_identifier(context, security)
        if not identifier:
            context["manual_review"] = True
            context["identity_resolution"] = "canonical_identifier_missing"
            resolved.append(
                replace(
                    finding, remediation_strategy="unsupported", context=context
                )
            )
            continue
        context.update(
            security_id=str(security.id),
            identifier=identifier,
            identifier_type=identifier_type,
            identity_resolution="canonical",
        )
        resolved.append(replace(finding, context=context))
    return resolved


async def _find_canonical_security(
    session: Any, context: dict[str, Any]
) -> Any | None:
    security_id = context.get("security_id")
    if security_id:
        rows = list(
            (
                await session.execute(
                    select(Security).where(Security.id == str(security_id))
                )
            ).scalars()
        )
        return rows[0] if len(rows) == 1 else None
    identifier = context.get("identifier")
    identifier_type = str(context.get("identifier_type", "")).lower()
    if not identifier:
        return None
    value = str(identifier).strip()
    if not value or identifier_type not in {
        "isin",
        "ticker",
        "figi",
        "cusip",
        "provider_symbol",
    }:
        return None
    if identifier_type == "provider_symbol":
        statements = (
            select(Security).where(
                func.upper(Security.ticker) == value.upper()
            ),
            select(Security)
            .join(SecurityListing, SecurityListing.security_id == Security.id)
            .where(func.upper(SecurityListing.ticker) == value.upper()),
        )
        rows: list[Any] = []
        for statement in statements:
            rows.extend(list((await session.execute(statement)).scalars()))
        unique = {str(row.id): row for row in rows}
        return next(iter(unique.values())) if len(unique) == 1 else None
    column = getattr(Security, identifier_type)
    rows = list(
        (
            await session.execute(
                select(Security).where(func.upper(column) == value.upper())
            )
        ).scalars()
    )
    return rows[0] if len(rows) == 1 else None


def _canonical_identifier(
    context: dict[str, Any], security: Any
) -> tuple[str | None, str | None]:
    explicit = context.get("identifier")
    explicit_type = str(context.get("identifier_type", "")).lower()
    if explicit and explicit_type in {
        "isin",
        "ticker",
        "figi",
        "cusip",
        "provider_symbol",
    }:
        return str(explicit), explicit_type
    for attribute, kind in (
        ("isin", "isin"),
        ("ticker", "ticker"),
        ("figi", "figi"),
        ("cusip", "cusip"),
    ):
        value = getattr(security, attribute, None)
        if value:
            return str(value), kind
    return None, None


def normalize_health_issues(
    payload: dict[str, Any], *, tenant_id: str, target_id: str
) -> list[DetectedIssue]:
    """Convert tolerant Wealthfolio issue shapes into bounded findings."""
    raw_issues = payload.get("issues", [])
    if not isinstance(raw_issues, list):
        return [
            DetectedIssue(
                tenant_id=tenant_id,
                provider_key=PROVIDER_KEY,
                issue_type="wealthfolio_unsupported_issue",
                affected_entity_type="health_response",
                affected_entity_id="malformed-issues",
                severity="error",
                remediation_strategy="unsupported",
                context={"target_id": target_id, "reason": "issues_not_array"},
                connection_id=target_id,
                scope=target_id,
            )
        ]
    raw_issues = cast("list[object]", raw_issues)
    findings: list[DetectedIssue] = []
    for raw in raw_issues[:MAX_AFFECTED_ITEMS]:
        if not isinstance(raw, dict):
            continue
        raw = cast("dict[str, Any]", raw)
        kind = _issue_kind(raw)
        severity = _text(raw.get("severity"), limit=16).lower()
        if severity not in {"info", "warning", "error"}:
            severity = "warning"
        affected = raw.get("affectedItems", raw.get("affected_items", []))
        if not isinstance(affected, list) or not affected:
            affected = [
                raw.get("assetId") or raw.get("securityId") or "summary"
            ]
        affected = cast("list[object]", affected)
        for item in affected[:MAX_AFFECTED_ITEMS]:
            if isinstance(item, dict):
                item = cast("dict[str, Any]", item)
                entity_id = (
                    item.get("id")
                    or item.get("assetId")
                    or item.get("asset_id")
                    or item.get("symbol")
                )
                entity_type = item.get("type") or "wealthfolio_asset"
            else:
                entity_id = item
                entity_type = "wealthfolio_asset"
            entity_id = _text(entity_id) or "summary"
            context = {
                "target_id": target_id,
                "remote_entity_id": entity_id,
                "code": _text(raw.get("code"), limit=64),
                "fix_action": _text(raw.get("fixAction"), limit=64),
                "details": _text(raw.get("details") or raw.get("message")),
            }
            if isinstance(item, dict):
                item_data: Any = cast("Any", item)
                for source_key in ("securityId", "security_id"):
                    if item_data.get(source_key) is not None:
                        context["security_id"] = _text(
                            item_data[source_key], limit=64
                        )
                        break
                if item_data.get("isin", item_data.get("ISIN")) is not None:
                    context["identifier"] = _text(
                        item_data.get("isin", item_data.get("ISIN")), limit=64
                    )
                    context["identifier_type"] = "isin"
                elif (
                    item_data.get("ticker", item_data.get("Ticker")) is not None
                ):
                    context["identifier"] = _text(
                        item_data.get("ticker", item_data.get("Ticker")),
                        limit=64,
                    )
                    context["identifier_type"] = "ticker"
                elif item_data.get("figi") is not None:
                    context["identifier"] = _text(item_data["figi"], limit=64)
                    context["identifier_type"] = "figi"
                elif item_data.get("cusip") is not None:
                    context["identifier"] = _text(item_data["cusip"], limit=64)
                    context["identifier_type"] = "cusip"
                elif (
                    item_data.get(
                        "providerSymbol", item_data.get("provider_symbol")
                    )
                    is not None
                ):
                    context["identifier"] = _text(
                        item_data.get(
                            "providerSymbol", item_data.get("provider_symbol")
                        ),
                        limit=64,
                    )
                    context["identifier_type"] = "provider_symbol"
                elif item_data.get("symbol") is not None:
                    context["identifier"] = _text(item_data["symbol"], limit=64)
                    context["identifier_type"] = "ticker"
                for source_key, context_key in (
                    ("startDate", "start_date"),
                    ("endDate", "end_date"),
                ):
                    if item_data.get(source_key) is not None:
                        context[context_key] = _text(
                            item_data[source_key], limit=64
                        )
            if kind not in {
                "wealthfolio_quote_sync_failure",
                "wealthfolio_historical_price_gap",
            }:
                context["manual_review"] = True
            findings.append(
                DetectedIssue(
                    tenant_id=tenant_id,
                    provider_key=PROVIDER_KEY,
                    issue_type=kind,
                    affected_entity_type=_text(entity_type, limit=64),
                    affected_entity_id=entity_id,
                    severity=severity,
                    priority=10 if severity == "error" else 0,
                    remediation_strategy=_strategy(kind),
                    context=context,
                    target_id=target_id,
                    scope=target_id,
                )
            )
    return findings


def health_payload_hash(payload: dict[str, Any]) -> str:
    """Hash normalized JSON for cursor idempotency without storing payload."""
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


class WealthfolioHealthBridge:
    """Poll result persistence and backlog lifecycle for one tenant."""

    def __init__(
        self, session: Any, tenant_id: str, target: ExportTarget
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.target = target

    async def enqueue_success(
        self,
        payload: dict[str, Any],
        *,
        complete: bool | None = None,
        truncated: bool = False,
        cursor_state: dict[str, Any] | None = None,
        compatibility_verified: bool = False,
    ) -> list[DataQualityRemediationItem]:
        now = datetime.now(UTC)
        findings = normalize_health_issues(
            payload, tenant_id=self.tenant_id, target_id=str(self.target.id)
        )
        findings = await resolve_canonical_health_issues(self.session, findings)
        if not compatibility_verified:
            findings = [
                replace(
                    finding,
                    remediation_strategy=(
                        "unsupported"
                        if finding.remediation_strategy
                        in {"wealthfolio_quote", "wealthfolio_price_history"}
                        else finding.remediation_strategy
                    ),
                    context={
                        **finding.context,
                        "manual_review": (
                            finding.remediation_strategy
                            in {
                                "wealthfolio_quote",
                                "wealthfolio_price_history",
                            }
                        ),
                        "compatibility": "unsupported_or_unverified",
                    },
                )
                for finding in findings
            ]
        result = await self.session.execute(
            select(WealthfolioHealthCursor).where(
                WealthfolioHealthCursor.tenant_id == self.tenant_id,
                WealthfolioHealthCursor.target_id == self.target.id,
            )
        )
        cursor = result.scalar_one_or_none()
        if cursor is None:
            cursor = WealthfolioHealthCursor(
                tenant_id=self.tenant_id, target_id=self.target.id
            )
            self.session.add(cursor)
        if complete is None:
            complete = (
                isinstance(payload.get("issues"), list) and "issues" in payload
            )
        complete = bool(complete) and isinstance(payload.get("issues"), list)
        cursor.last_successful_poll = now
        cursor.payload_hash = health_payload_hash(payload)
        cursor.issue_count = len(findings)
        cursor.complete = complete
        cursor.truncated = bool(truncated)
        cursor.cursor_state = dict(cursor_state or {})
        cursor.last_error = None
        cursor.last_error_category = None
        backlog = BacklogRepository(self.session)
        items = [await backlog.register(issue) for issue in findings]
        current_keys = {deduplication_key(issue) for issue in findings}
        active = (
            await self.session.execute(
                select(DataQualityRemediationItem).where(
                    DataQualityRemediationItem.tenant_id == self.tenant_id,
                    DataQualityRemediationItem.provider_key == PROVIDER_KEY,
                    DataQualityRemediationItem.status.in_(
                        ("pending", "retry_wait", "deferred")
                    ),
                    DataQualityRemediationItem.context["target_id"].as_string()
                    == str(self.target.id),
                )
            )
        ).scalars()
        for item in active:
            if (
                complete
                and not truncated
                and item.deduplication_key not in current_keys
                and item.issue_type.startswith("wealthfolio_")
            ):
                await backlog.transition(
                    self.tenant_id,
                    str(item.id),
                    status="resolved",
                    allowed_from=("pending", "retry_wait", "deferred"),
                )
        await self.session.flush()
        return items

    async def record_failure(self, *, category: str, message: str) -> None:
        result = await self.session.execute(
            select(WealthfolioHealthCursor).where(
                WealthfolioHealthCursor.tenant_id == self.tenant_id,
                WealthfolioHealthCursor.target_id == self.target.id,
            )
        )
        cursor = result.scalar_one_or_none()
        if cursor is None:
            cursor = WealthfolioHealthCursor(
                tenant_id=self.tenant_id, target_id=self.target.id
            )
            self.session.add(cursor)
        cursor.complete = False
        cursor.truncated = False
        cursor.cursor_state = {"reason": "poll_failed"}
        cursor.last_error_category = _text(category, limit=32)
        cursor.last_error = _text(message, limit=MAX_CONTEXT_TEXT)
        await self.session.flush()
