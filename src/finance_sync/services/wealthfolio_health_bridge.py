"""Normalize Wealthfolio health findings into the remediation backlog."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from finance_sync.models import ExportTarget, WealthfolioHealthCursor
from finance_sync.models.remediation import DataQualityRemediationItem
from finance_sync.reconciliation.remediation.backlog import (
    BacklogRepository,
    DetectedIssue,
)

PROVIDER_KEY = "wealthfolio"
MAX_CONTEXT_TEXT = 256
MAX_AFFECTED_ITEMS = 100


def _text(value: object, *, limit: int = MAX_CONTEXT_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _issue_kind(issue: dict[str, Any]) -> str:
    raw = " ".join(
        _text(issue.get(key)).lower()
        for key in ("code", "category", "type", "fixAction")
    )
    if any(token in raw for token in ("quote", "price", "market_data")):
        if "histor" in raw or "gap" in raw or "missing_price" in raw:
            return "wealthfolio_historical_price_gap"
        return "wealthfolio_quote_sync_failure"
    if "purchase" in raw or "cost_basis" in raw:
        return "wealthfolio_missing_purchase_price"
    if "negative" in raw and "valuation" in raw:
        return "wealthfolio_negative_valuation"
    if "incomplete" in raw and "valuation" in raw:
        return "wealthfolio_incomplete_valuation"
    return "wealthfolio_unsupported_issue"


def _strategy(kind: str) -> str:
    if kind == "wealthfolio_quote_sync_failure":
        return "wealthfolio_quote"
    if kind == "wealthfolio_historical_price_gap":
        return "wealthfolio_price_history"
    return "unsupported"


def normalize_health_issues(
    payload: dict[str, Any], *, tenant_id: str, target_id: str
) -> list[DetectedIssue]:
    """Convert tolerant Wealthfolio issue shapes into bounded stable findings."""
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
            )
        ]
    findings: list[DetectedIssue] = []
    for raw in raw_issues[:MAX_AFFECTED_ITEMS]:
        if not isinstance(raw, dict):
            continue
        kind = _issue_kind(raw)
        severity = _text(raw.get("severity"), limit=16).lower()
        if severity not in {"info", "warning", "error"}:
            severity = "warning"
        affected = raw.get("affectedItems", raw.get("affected_items", []))
        if not isinstance(affected, list) or not affected:
            affected = [raw.get("assetId") or raw.get("securityId") or "summary"]
        for item in affected[:MAX_AFFECTED_ITEMS]:
            if isinstance(item, dict):
                entity_id = item.get("id") or item.get("assetId") or item.get("symbol")
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
                )
            )
    return findings


def health_payload_hash(payload: dict[str, Any]) -> str:
    """Hash normalized JSON for cursor idempotency without storing payload."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


class WealthfolioHealthBridge:
    """Poll result persistence and backlog lifecycle for one tenant."""

    def __init__(self, session: Any, tenant_id: str, target: ExportTarget) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.target = target

    async def enqueue_success(self, payload: dict[str, Any]) -> list[DataQualityRemediationItem]:
        now = datetime.now(UTC)
        findings = normalize_health_issues(
            payload, tenant_id=self.tenant_id, target_id=str(self.target.id)
        )
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
        cursor.last_successful_poll = now
        cursor.payload_hash = health_payload_hash(payload)
        cursor.issue_count = len(findings)
        cursor.last_error = None
        cursor.last_error_category = None
        backlog = BacklogRepository(self.session)
        items = [await backlog.register(issue) for issue in findings]
        current_keys = {issue.deduplication_key for issue in findings}
        active = (
            await self.session.execute(
                select(DataQualityRemediationItem).where(
                    DataQualityRemediationItem.tenant_id == self.tenant_id,
                    DataQualityRemediationItem.provider_key == PROVIDER_KEY,
                    DataQualityRemediationItem.status.in_(("pending", "retry_wait", "deferred")),
                    DataQualityRemediationItem.context["target_id"].as_string()
                    == str(self.target.id),
                )
            )
        ).scalars()
        for item in active:
            if item.deduplication_key not in current_keys and item.issue_type.startswith("wealthfolio_"):
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
        cursor.last_error_category = _text(category, limit=32)
        cursor.last_error = _text(message, limit=MAX_CONTEXT_TEXT)
        await self.session.flush()
