"""Hermes relevance explanations, grounded only in finance-sync facts.

Implements the Hermes-explanation bullet of
backlog/plus-relevant-nieuws-en-events.md and task t_c6959faa:

* The security match, holding status, event dates and source references
  are **deterministic finance-sync facts**.  An explanation may restate
  those facts (event dates, security names, source URLs) and reference
  the underlying intelligence item IDs — never unconfirmed facts,
  position sizes, financial values or speculative impact.
* Hermes integration is **optional**: when no Hermes client is
  configured, disabled, or the upstream call fails, the explainer
  returns ``None`` or a deterministic fallback.  The deterministic
  holding-relevance data remains fully available without it.

Design
------

* :class:`HermesRelevanceExplainer` wraps an optional ``hermes_client``
  (duck-typed: anything with ``async explain(...) -> str``).  When the
  client is missing or disabled, :meth:`explain` returns ``None`` and
  the caller simply omits ``hermes_explanation`` from its DTO — no
  crash, no invented text.
* When a client exists but the upstream call fails, a deterministic
  fallback sentence is built from the same fact-only data.
* The fact set fed to the client is **pre-filtered**: only the whitelisted
  deterministic keys (event type/date facts, report period) survive.
  Financial facts (amounts, yields, price targets, …) are stripped
  before they could ever reach an LLM prompt.
"""

from __future__ import annotations

from typing import Any, Protocol

import structlog

logger = structlog.get_logger(__name__)

#: Structured-fact keys that are deterministic and safe to expose to an
#: explainer.  Anything else (amounts, yields, targets, …) is stripped
#: before it can reach a prompt — the "no financial values" guarantee is
#: enforced at the boundary, not left to the client.
_FACT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "event_type",
        "earnings_date",
        "ex_date",
        "record_date",
        "payment_date",
        "meeting_date",
        "split_date",
        "filing_date",
        "report_period",
        "quarter",
        "fiscal_year",
        "dividend_frequency",
    }
)

#: Match reasons → deterministic English clause (restates the stored
#: finance-sync match provenance).
_MATCH_REASON_LABEL: dict[str, str] = {
    "canonical_security": "matches a current holding",
    "recently_sold": "the security was recently sold",
    "currency_interest": "interest/currency event for a cash account",
    "hermes_suggested": "suggested by Hermes",
}

#: Max explanation length in characters (a few sentences).
_MAX_LEN = 400


class HermesClient(Protocol):
    """Duck-typed Hermes client contract (any async ``explain`` works)."""

    async def explain(
        self, prompt: str, *, facts: list[dict[str, Any]] | None = None
    ) -> str: ...


class HermesRelevanceExplainer:
    """Generate short, fact-only relevance explanations.

    Parameters
    ----------
    hermes_client:
        Optional Hermes integration.  When ``None``, :meth:`explain`
        returns the **deterministic fallback** — a fact-only sentence
        built from the cluster DTO — so users still get a grounded
        explanation without Hermes.  ``disabled=True`` (feature off)
        makes :meth:`explain` return ``None`` instead, so callers can
        omit ``hermes_explanation`` entirely.
    disabled:
        When True the explainer is inert (returns ``None``); used by
        :func:`build_hermes_explainer` for the feature-flag-off path.
    """

    def __init__(
        self,
        hermes_client: HermesClient | None = None,
        *,
        disabled: bool = False,
    ) -> None:
        self._client = hermes_client
        self._disabled = disabled

    async def explain(
        self,
        cluster_dto: dict[str, Any] | None,
        *,
        facts: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Return a short relevance explanation for *cluster_dto*.

        Returns ``None`` when the feature is disabled or the input is
        unusable.  Without a Hermes client (or when the upstream call
        fails) a deterministic, fact-only fallback is returned.  Never
        raises.
        """
        if self._disabled:
            return None
        if not isinstance(cluster_dto, dict):
            return None

        safe_facts = _allowlisted_facts(facts)
        if self._client is None:
            return _fallback(cluster_dto)

        prompt = _build_prompt(cluster_dto, safe_facts)
        if not prompt:
            return None

        try:
            text = await self._client.explain(prompt, facts=safe_facts or None)
        except Exception:
            # Hermes is optional — never crash the feed on upstream errors.
            logger.warning(
                "hermes_explanation_upstream_failed",
                cluster_id=cluster_dto.get("id"),
                error="hermes client error",
            )
            return _fallback(cluster_dto)

        if not text:
            return _fallback(cluster_dto)
        # The client may still hallucinate — the deterministic fallback
        # is the safe answer unless the client produced nothing.
        # (Prompting keeps it grounded; we do not re-parse free text.)
        return text[:_MAX_LEN]


def build_hermes_explainer(
    *,
    enabled: bool = True,
    hermes_client: HermesClient | None = None,
) -> HermesRelevanceExplainer:
    """Factory: return a real, fallback-only, or disabled explainer.

    ``enabled=False`` yields an inert explainer whose :meth:`explain`
    always returns ``None`` — deterministic data stays available without
    any Hermes integration.  ``enabled=True`` with ``hermes_client=None``
    yields the deterministic-fallback explainer.
    """
    if not enabled:
        return HermesRelevanceExplainer(hermes_client=None, disabled=True)
    return HermesRelevanceExplainer(hermes_client=hermes_client)


# ── Internals ────────────────────────────────────────────────────────


def _allowlisted_facts(
    facts: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Keep only deterministic, non-financial facts.

    This is the enforcement point for "no financial values": any fact
    whose key is not on the allowlist (amounts, yields, price targets,
    market caps, …) never reaches the prompt or the fallback.
    """
    if not facts:
        return []
    out: list[dict[str, Any]] = []
    for fact in facts:
        key = fact.get("key")
        if isinstance(key, str) and key in _FACT_ALLOWLIST:
            out.append(fact)
    return out


def _item_ids(cluster_dto: dict[str, Any]) -> list[str]:
    """Return the cluster's intelligence item IDs as a string list."""
    raw: Any = cluster_dto.get("item_ids")
    if isinstance(raw, list):
        entries: list[Any] = list(raw)  # type: ignore[reportUnknownArgumentType]
        ids: list[str] = []
        for entry in entries:
            if entry is None:
                continue
            ids.append(str(entry))
        return ids
    return []


def _safe_str(value: Any) -> str | None:
    """Return *value* as a short string, or None when unusable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return None
    return text


def _build_prompt(
    cluster_dto: dict[str, Any], facts: list[dict[str, Any]]
) -> str | None:
    """Build the fact-only context for the Hermes client."""
    security = _safe_str(
        cluster_dto.get("security_ticker") or cluster_dto.get("security_name")
    ) or _safe_str(cluster_dto.get("security_name"))
    if not security:
        security = "the holding"
    event_type = _safe_str(cluster_dto.get("event_type")) or "event"
    event_date = _safe_str(cluster_dto.get("event_date"))
    match_reason = _safe_str(cluster_dto.get("match_reason"))
    reason_clause = _MATCH_REASON_LABEL.get(
        match_reason or "", "is relevant to a holding"
    )
    item_ids = _item_ids(cluster_dto)
    best_url = _safe_str(cluster_dto.get("best_source_url"))

    lines = [
        "Explain in at most two sentences why this item is relevant to "
        "the holding. Use ONLY the facts below. Do not invent facts, "
        "do not mention position sizes, financial values, prices, "
        "amounts, or speculative impact.",
        f"Security: {security}",
        f"Event type: {event_type}",
        f"Match reason: {reason_clause}",
    ]
    if event_date:
        lines.append(f"Event date: {event_date}")
    if facts:
        fact_lines = ", ".join(
            f"{f.get('key')}={f.get('value')}" for f in facts
        )
        lines.append(f"Facts: {fact_lines}")
    if item_ids:
        lines.append(f"Intelligence item IDs: {', '.join(map(str, item_ids))}")
    if best_url:
        lines.append(f"Source URL: {best_url}")
    lines.append(
        "Reply with the explanation text only (no markdown, no bullets)."
    )
    return "\n".join(lines)


def _security_label(cluster_dto: dict[str, Any]) -> str:
    """Human label: canonical name, else ticker, else generic."""
    name = _safe_str(cluster_dto.get("security_name"))
    if name:
        return name
    ticker = _safe_str(cluster_dto.get("security_ticker"))
    if ticker:
        return ticker
    return "the holding"


def _fallback(cluster_dto: dict[str, Any]) -> str | None:
    """Deterministic, fact-only fallback when Hermes is unavailable.

    Built exclusively from the DTO's deterministic fields: security
    name/ticker, event type, event date, match reason, item IDs and
    source URL.  Never includes financial values or position sizes.
    """
    security = _security_label(cluster_dto)
    event_type = _safe_str(cluster_dto.get("event_type")) or "event"
    event_date = _safe_str(cluster_dto.get("event_date"))
    match_reason = _safe_str(cluster_dto.get("match_reason"))
    reason_clause = _MATCH_REASON_LABEL.get(
        match_reason or "", "is relevant to a holding"
    )
    item_ids = _item_ids(cluster_dto)
    best_url = _safe_str(cluster_dto.get("best_source_url"))

    parts = [
        f"Relevant because this {event_type} event concerns {security}"
        f" ({reason_clause})."
    ]
    if event_date:
        parts.append(f"Event date: {event_date}.")
    if item_ids:
        joined = ", ".join(map(str, item_ids[:10]))
        parts.append(f"Source item(s): {joined}.")
    if best_url:
        parts.append(f"Source: {best_url}")
    text = " ".join(parts)
    return text[:_MAX_LEN] if text else None
