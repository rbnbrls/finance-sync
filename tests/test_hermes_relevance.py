"""Tests for the Hermes relevance explainer.

Implements the acceptance criteria of task t_c6959faa
(backlog/plus-relevant-nieuws-en-events.md, Hermes-explanation bullet):

* An explanation is **deterministic** and grounded only in finance-sync
  facts: the security match (ticker/name), the event type + event date,
  the cluster's source items (referenced by their item IDs), and the
  source URLs.  No unconfirmed facts, no speculative impact.
* When Hermes integration is unavailable (no client, disabled, or the
  upstream call fails), the explainer returns ``None`` or a
  deterministic fallback — it never crashes and never invents facts.
* No explanation ever contains financial values or position sizes
  (no quantities, market values, prices, weights, or confidence
  numbers that imply a position).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from finance_sync.services.hermes_relevance import (
    HermesRelevanceExplainer,
    build_hermes_explainer,
)

# ═══════════════════════════════════════════════════════════════════════
# Fixtures / helpers
# ═══════════════════════════════════════════════════════════════════════


def _cluster_dto(
    *,
    item_ids: list[str] | None = None,
    security_ticker: str | None = "AAPL",
    security_name: str | None = "Apple Inc.",
    event_type: str = "earnings",
    event_date: datetime | None = None,
    headline: str = "Apple beats Q4 earnings estimates",
    sources: list[dict[str, Any]] | None = None,
    match_reason: str | None = "canonical_security",
    confidence: float | None = 1.0,
) -> dict[str, Any]:
    """Build a feed DTO-shaped dict (as produced by the service)."""
    ids = item_ids or ["item-1"]
    return {
        "id": "cluster-1",
        "cluster_id": "cluster-1",
        "security_id": "sec-1",
        "security_ticker": security_ticker,
        "security_name": security_name,
        "event_type": event_type,
        "event_date": event_date or datetime(2026, 9, 3, 20, 0, tzinfo=UTC),
        "headline": headline,
        "score": 0.42,
        "match_reason": match_reason,
        "confidence": confidence,
        "is_stale": False,
        "source_count": len(ids),
        "cluster_reason": "exact_event",
        "earliest_published_at": datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        "item_ids": ids,
        "best_source_url": "https://example.com/news/1",
        "acknowledged": False,
        "sources": sources
        or [
            {
                "item_id": ids[0],
                "provider": "openbb",
                "source_id": "src-1",
                "url": "https://example.com/news/1",
                "headline": headline,
                "published_at": datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
                "fetched_at": datetime(2026, 9, 3, 12, 5, tzinfo=UTC),
                "freshness": "fresh",
                "license_class": "free_access",
            }
        ],
    }


def _facts() -> list[dict[str, Any]]:
    """Structured earnings facts (deterministic finance-sync facts)."""
    return [
        {"key": "event_type", "value": "earnings"},
        {"key": "earnings_date", "value": "2026-09-03"},
        {"key": "report_period", "value": "Q4 2026"},
    ]


@pytest.fixture
def explainer() -> HermesRelevanceExplainer:
    """A HermesRelevanceExplainer with no Hermes client (deterministic)."""
    return HermesRelevanceExplainer(hermes_client=None)


# ═══════════════════════════════════════════════════════════════════════
# B1 — Known earnings-date match → fact-only explanation with item IDs
# ═══════════════════════════════════════════════════════════════════════


class TestKnownEarningsMatch:
    async def test_explains_earnings_match_with_facts_and_item_ids(
        self, explainer: HermesRelevanceExplainer
    ) -> None:
        """A known earnings-date match produces a fact-only explanation."""
        dto = _cluster_dto()
        explanation = await explainer.explain(dto, facts=_facts())

        assert explanation is not None
        # References the underlying intelligence item ID(s).
        assert "item-1" in explanation
        # Restates deterministic facts only: security name, event type,
        # event date.  No financial values / position sizes.
        assert "Apple" in explanation
        assert "earnings" in explanation.lower()
        assert "2026" in explanation
        # At most a few sentences (period followed by a capital/end —
        # abbreviations like "Inc." are not sentence boundaries).
        import re

        sentence_enders = re.findall(r"\.(?=\s+[A-Z]|$)", explanation)
        assert len(sentence_enders) <= 4

    async def test_explanation_mentions_each_item_id(
        self, explainer: HermesRelevanceExplainer
    ) -> None:
        """Every source item ID in the cluster is referenced."""
        dto = _cluster_dto(item_ids=["item-1", "item-2", "item-3"])
        explanation = await explainer.explain(dto, facts=_facts())

        assert explanation is not None
        for item_id in ("item-1", "item-2", "item-3"):
            assert item_id in explanation

    async def test_no_financial_values_or_position_sizes(
        self, explainer: HermesRelevanceExplainer
    ) -> None:
        """Explanations never contain financial values or position sizes."""
        dto = _cluster_dto(
            # The DTO carries weight/score/confidence — none may leak.
            sources=[
                {
                    "item_id": "item-1",
                    "provider": "openbb",
                    "source_id": "src-1",
                    "url": "https://example.com/news/1",
                    "headline": "Apple beats Q4 earnings estimates",
                    "published_at": datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
                    "fetched_at": datetime(2026, 9, 3, 12, 5, tzinfo=UTC),
                    "freshness": "fresh",
                    "license_class": "free_access",
                }
            ],
        )
        dto["score"] = 0.99
        dto["confidence"] = 1.0
        explanation = await explainer.explain(dto, facts=_facts())

        assert explanation is not None
        forbidden = ("0.99", "1.0", "€", "$", "position", "market value")
        for token in forbidden:
            assert token not in explanation

    async def test_explanation_is_deterministic(
        self, explainer: HermesRelevanceExplainer
    ) -> None:
        """Same input always yields the same explanation."""
        dto = _cluster_dto(item_ids=["item-1", "item-2"])
        a = await explainer.explain(dto, facts=_facts())
        b = await explainer.explain(dto, facts=_facts())
        assert a == b

    async def test_dividend_event_explains_ex_date(
        self, explainer: HermesRelevanceExplainer
    ) -> None:
        """A dividend event restates the ex-date fact, no value."""
        dto = _cluster_dto(
            event_type="dividend",
            event_date=datetime(2026, 9, 15, 0, 0, tzinfo=UTC),
            headline="Apple declares quarterly dividend",
        )
        facts = [
            {"key": "event_type", "value": "dividend"},
            {"key": "ex_date", "value": "2026-09-15"},
            {"key": "payment_date", "value": "2026-10-05"},
        ]
        explanation = await explainer.explain(dto, facts=facts)

        assert explanation is not None
        assert "dividend" in explanation.lower()
        assert "2026-09-15" in explanation
        # Never invents an amount.
        assert "$" not in explanation

    async def test_source_url_restated(
        self, explainer: HermesRelevanceExplainer
    ) -> None:
        """The explanation may restate the source URL."""
        dto = _cluster_dto()
        explanation = await explainer.explain(dto, facts=_facts())
        assert explanation is not None
        assert "https://example.com/news/1" in explanation


# ═══════════════════════════════════════════════════════════════════════
# B2 — Hermes unavailable → None or deterministic fallback, never crash
# ═══════════════════════════════════════════════════════════════════════


class TestHermesUnavailable:
    async def test_no_client_returns_none_or_fallback(
        self, explainer: HermesRelevanceExplainer
    ) -> None:
        """With Hermes unavailable the function degrades gracefully."""
        dto = _cluster_dto()
        result = await explainer.explain(dto, facts=_facts())

        # Either None (Hermes off) or a deterministic fallback — never an
        # exception, never a fabricated explanation.
        assert result is None or isinstance(result, str)

    async def test_explain_missing_item_ids_does_not_crash(
        self, explainer: HermesRelevanceExplainer
    ) -> None:
        """A malformed DTO (no item ids / no sources) degrades safely."""
        dto = _cluster_dto(item_ids=[], sources=[])
        result = await explainer.explain(dto, facts=_facts())
        assert result is None or isinstance(result, str)

    async def test_upstream_failure_returns_deterministic_fallback(
        self,
    ) -> None:
        """A failing Hermes client returns a fallback, never crashes."""

        class _Boom:
            async def explain(self, *_args: Any, **_kwargs: Any) -> str:
                error = "upstream unreachable"
                raise RuntimeError(error)

        svc = HermesRelevanceExplainer(hermes_client=_Boom())  # type: ignore[arg-type]
        result = await svc.explain(_cluster_dto(), facts=_facts())
        assert result is not None
        assert isinstance(result, str)
        assert "item-1" in result

    async def test_disabled_via_factory_returns_none(self) -> None:
        """build_hermes_explainer(False) yields a None-returning service."""
        svc = build_hermes_explainer(enabled=False)
        result = await svc.explain(_cluster_dto(), facts=_facts())
        assert result is None

    async def test_fallback_never_contains_values(
        self,
    ) -> None:
        """Even the fallback path stays free of financial values."""

        class _Boom:
            async def explain(self, *_args: Any, **_kwargs: Any) -> str:
                error = "upstream unreachable"
                raise RuntimeError(error)

        dto = _cluster_dto()
        dto["score"] = 0.99
        svc = HermesRelevanceExplainer(hermes_client=_Boom())  # type: ignore[arg-type]
        result = await svc.explain(dto, facts=_facts())
        assert result is not None
        for token in ("0.99", "1.0", "€", "$", "position", "market value"):
            assert token not in result


# ═══════════════════════════════════════════════════════════════════════
# B3 — Fact-only guarantee: no unconfirmed facts, no speculation
# ═══════════════════════════════════════════════════════════════════════


class TestFactOnly:
    async def test_missing_facts_does_not_guess(
        self, explainer: HermesRelevanceExplainer
    ) -> None:
        """Without structured facts the explanation uses only the DTO."""
        dto = _cluster_dto()
        explanation = await explainer.explain(dto, facts=None)

        assert explanation is not None
        # No invented period/beat claims.
        assert "beat" not in explanation.lower()
        assert "estimate" not in explanation.lower()
        assert "expected" not in explanation.lower()

    async def test_explanation_does_not_repeat_headline(
        self, explainer: HermesRelevanceExplainer
    ) -> None:
        """The explanation is a derived sentence, not a headline copy."""
        dto = _cluster_dto(
            headline="Apple beats Q4 earnings estimates",
            event_type="news",
            event_date=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        )
        explanation = await explainer.explain(dto, facts=[])
        assert explanation is not None
        # A pure headline echo would be a restatement without reasoning;
        # the explainer must produce its own grounded sentence instead.
        assert explanation != dto["headline"]

    async def test_recently_sold_reason_restates_status(
        self, explainer: HermesRelevanceExplainer
    ) -> None:
        """A recently-sold match explains the deterministic reason."""
        dto = _cluster_dto(match_reason="recently_sold", confidence=0.8)
        explanation = await explainer.explain(dto, facts=_facts())
        assert explanation is not None
        assert (
            "recently sold" in explanation.lower()
            or "verkocht" in explanation.lower()
        )


# ═══════════════════════════════════════════════════════════════════════
# B4 — Integration with the feed DTO (hermes_explanation key present)
# ═══════════════════════════════════════════════════════════════════════


class TestFeedDTOIntegration:
    async def test_explainer_rejects_non_dto_input_gracefully(
        self, explainer: HermesRelevanceExplainer
    ) -> None:
        """Non-dict input degrades to None instead of crashing."""
        assert await explainer.explain(None, facts=_facts()) is None  # type: ignore[arg-type]
        assert await explainer.explain("nope", facts=_facts()) is None  # type: ignore[arg-type]

    async def test_explanation_len_cap(
        self, explainer: HermesRelevanceExplainer
    ) -> None:
        """Explanations stay at most a few sentences (<= 400 chars)."""
        many_ids = [f"item-{i}" for i in range(1, 21)]
        dto = _cluster_dto(item_ids=many_ids)
        explanation = await explainer.explain(dto, facts=_facts())
        assert explanation is None or len(explanation) <= 400
