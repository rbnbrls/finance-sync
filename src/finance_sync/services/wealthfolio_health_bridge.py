"""Normalize Wealthfolio health findings into the remediation backlog."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select

from finance_sync.models import (
    Account,
    ExportTarget,
    Holding,
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
    capability_response = cast("dict[str, Any]", capability_response)
    # Current Wealthfolio health responses omit version/capability metadata.
    # The worker supplies this opt-in marker only after an authenticated read
    # of the supported assets contract; arbitrary health JSON stays fail-safe.
    if capability_response.get("client_contract_verified") is True:
        return True
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

    issues = cast("list[Any]", issues)
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
    fix_action = issue.get("fixAction", issue.get("fix_action"))
    fix_action_id = (
        fix_action.get("id", "") if isinstance(fix_action, dict) else fix_action
    )
    raw = " ".join(
        _text(issue.get(key)).lower()
        for key in (
            "code",
            "category",
            "type",
            "title",
            "description",
            "details",
        )
    )
    raw = f"{raw} {_text(fix_action_id).lower()}"
    if any(token in raw for token in ("fx", "exchange rate", "exchange_rate")):
        return "wealthfolio_fx_sync"
    # Unsafe financial findings must win over generic ``price`` wording in
    # codes such as MISSING_PURCHASE_PRICE.  They have no safe automatic
    # repair primitive and therefore remain manual-review-only.
    if "purchase" in raw or "cost_basis" in raw or "cost basis" in raw:
        return "wealthfolio_missing_purchase_price"
    if "negative" in raw and "valuation" in raw:
        return "wealthfolio_negative_valuation"
    # A valuation gap is historical coverage, not merely the latest quote.
    # Handle it before the generic ``sync_prices`` action so the workflow can
    # request the missing date window from the price-history strategy.
    if "incomplete" in raw and "valuation" in raw:
        return "wealthfolio_incomplete_valuation"
    # Current Wealthfolio versions report missing valuation values as a
    # ``sync_prices`` action rather than using the older price-gap codes.
    # This is safe to repair because finance-sync can fetch and verify market
    # data; it must not fabricate a valuation or cost basis.
    if "sync_prices" in raw or "quote_sync" in raw:
        return "wealthfolio_quote_sync_failure"
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
    if kind == "wealthfolio_incomplete_valuation":
        return "wealthfolio_price_history"
    if kind == "wealthfolio_fx_sync":
        return "wealthfolio_fx"
    if kind == "wealthfolio_missing_purchase_price":
        return "wealthfolio_cost_basis"
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
            "wealthfolio_cost_basis",
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
        if finding.remediation_strategy == "wealthfolio_cost_basis":
            account_rows = list(
                (
                    await session.execute(
                        select(Account.id, Account.connection_id)
                        .join(Holding, Holding.account_id == Account.id)
                        .where(
                            Account.tenant_id == finding.tenant_id,
                            Account.provider_key == "trading212",
                            Holding.tenant_id == finding.tenant_id,
                            Holding.security_id == security.id,
                            Holding.quantity != 0,
                        )
                        .distinct()
                    )
                ).all()
            )
            if len(account_rows) == 1:
                context.update(
                    account_id=str(account_rows[0][0]),
                    connection_id=(
                        str(account_rows[0][1]) if account_rows[0][1] else None
                    ),
                    provider_account_id=str(account_rows[0][0]),
                )
            else:
                context["manual_review"] = True
                context["cost_basis_resolution"] = (
                    "no_unique_trading212_account"
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
    raw_issues = cast("list[Any]", raw_issues)
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
        affected = cast("list[Any]", affected)
        for item in affected[:MAX_AFFECTED_ITEMS]:
            item_data: dict[str, Any] = (
                cast("dict[str, Any]", item) if isinstance(item, dict) else {}
            )
            if isinstance(item, dict):
                entity_id = (
                    item_data.get("id")
                    or item_data.get("assetId")
                    or item_data.get("asset_id")
                    or item_data.get("symbol")
                )
                entity_type = item_data.get("type") or "wealthfolio_asset"
            else:
                entity_id = item
                entity_type = "wealthfolio_asset"
            entity_id = _text(entity_id) or "summary"
            context = {
                "target_id": target_id,
                "remote_entity_id": entity_id,
                "code": _text(raw.get("code"), limit=64),
                "fix_action": _text(
                    (raw.get("fixAction", raw.get("fix_action")) or {}).get(
                        "id", ""
                    )
                    if isinstance(
                        raw.get("fixAction", raw.get("fix_action")), dict
                    )
                    else raw.get("fixAction", raw.get("fix_action")),
                    limit=64,
                ),
                "details": _text(raw.get("details") or raw.get("message")),
            }
            if isinstance(item, dict):
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
                elif any(
                    item_data.get(key) is not None
                    for key in (
                        "providerSymbol",
                        "provider_symbol",
                        "symbol",
                        "displayCode",
                        "instrumentSymbol",
                    )
                ):
                    symbol = next(
                        item_data[key]
                        for key in (
                            "providerSymbol",
                            "provider_symbol",
                            "symbol",
                            "displayCode",
                            "instrumentSymbol",
                        )
                        if item_data.get(key) is not None
                    )
                    context["identifier"] = _text(symbol, limit=64)
                    context["identifier_type"] = "provider_symbol"
                if (
                    kind == "wealthfolio_incomplete_valuation"
                    and "start_date" not in context
                ):
                    dates = re.findall(
                        r"\b\d{4}-\d{2}-\d{2}\b",
                        str(raw.get("details") or ""),
                    )
                    if dates:
                        context["start_date"] = min(dates)
                        context["end_date"] = (
                            (
                                datetime.fromisoformat(max(dates))
                                + timedelta(days=1)
                            )
                            .date()
                            .isoformat()
                        )
            if kind == "wealthfolio_fx_sync" and ":" in entity_id:
                from_currency, to_currency = entity_id.split(":", 1)
                context.update(
                    from_currency=from_currency.upper(),
                    to_currency=to_currency.upper(),
                )
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
                "wealthfolio_fx_sync",
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
