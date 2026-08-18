"""Provider-independent DTOs for the market-intelligence source layer.

Normalised, provider-agnostic payloads produced by adapters and
consumed by the ingestion service.  A single item always carries:

* provenance  — provider, source id, canonical URL/document id
* time       — publication time, fetch time, validity window
* language   — BCP-47-ish language tag
* licensing  — reuse class (drives what may be stored)
* integrity  — content hash (dedupe + tamper detection)
* content    — headline, short snippet, structured facts (never the
  full copyrighted article unless the license allows it)
"""

from __future__ import annotations

from datetime import (
    datetime,  # noqa: TC003 — needed by pydantic model_rebuild()
)
from typing import Any, Literal

from pydantic import BaseModel, Field

from finance_sync.intel.enums import (  # noqa: TC001 — needed at runtime
    IntelItemKind,
    IntelLicenseClass,
)


class IntelStructuredFact(BaseModel):
    """One structured, factual statement derived from a source item.

    Facts are facts, not prose — they are always safe to store even
    for proprietary sources, and they keep derived records traceable
    to the originating item via ``item_source_id`` / ``item_url``.
    """

    key: str = Field(description="Stable fact key, e.g. 'eps_estimate'")
    value: str | int | float | bool | None = Field(
        default=None, description="Typed fact value"
    )
    unit: str | None = Field(
        default=None, description="Unit, e.g. 'EUR', 'pct'"
    )
    as_of: datetime | None = Field(
        default=None, description="Fact reference timestamp"
    )
    item_source_id: str | None = Field(
        default=None, description="Originating item's source id"
    )
    item_url: str | None = Field(
        default=None, description="Canonical URL of the originating item"
    )


class IntelItem(BaseModel):
    """Normalised market-intelligence item from any provider."""

    # ── Provenance ──────────────────────────────────────────────────
    provider: str = Field(description="Provider key, e.g. 'openbb', 'sec'")
    source_id: str = Field(
        description="Provider-scoped stable item id (dedupe key)"
    )
    canonical_url: str | None = Field(
        default=None, description="Canonical URL / document id of the item"
    )
    kind: IntelItemKind = Field(description="Concrete item kind")

    # ── Time ────────────────────────────────────────────────────────
    published_at: datetime = Field(
        description="Publication time of the source item"
    )
    fetched_at: datetime = Field(
        description="When this adapter fetched the item"
    )
    valid_from: datetime | None = Field(
        default=None, description="Start of the item's validity window"
    )
    valid_until: datetime | None = Field(
        default=None, description="End of the item's validity window"
    )

    # ── Language / licensing / integrity ────────────────────────────
    language: str = Field(
        default="en", description="BCP-47-ish language tag of the item"
    )
    license_class: IntelLicenseClass = Field(
        description="Reuse class driving the storage policy"
    )
    license_uri: str | None = Field(
        default=None, description="Link to the license terms, if any"
    )
    license_text: str | None = Field(
        default=None,
        description=(
            "Raw license string as reported by the source (may be empty, "
            "unknown or deviant).  When set, the ingestion service "
            "classifies it with :func:`finance_sync.intel.licensing."
            "infer_license_class` and never stores full text for "
            "anything that is not an explicit open/public class."
        ),
    )
    content_hash: str = Field(
        description="SHA-256 over the item's canonical identity"
    )

    # ── Content ─────────────────────────────────────────────────────
    headline: str | None = Field(
        default=None, description="Title/headline (always storable)"
    )
    summary: str | None = Field(
        default=None, description="Short summary/snippet (policy-gated)"
    )
    body: str | None = Field(
        default=None, description="Full text (license-gated)"
    )
    facts: list[IntelStructuredFact] = Field(
        default_factory=list[IntelStructuredFact],
        description="Structured facts derived from the item",
    )
    provider_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific non-secret metadata",
    )

    # ── Security identity (resolution input) ────────────────────────
    identifiers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Candidate security identifiers found in the item, e.g. "
            "{'ticker': 'AAPL', 'isin': 'US0378331005'}.  Resolved via "
            "the existing FIGI/ISIN/ticker/listing pipeline."
        ),
    )
    identifier_type: Literal["ticker", "isin", "figi", "name"] = Field(
        default="ticker",
        description="Primary identifier type of ``identifiers``",
    )

    #: Storage policy hint derived from the license class.  Set by the
    #: adapter; enforced again by the ingestion service.
    store_full_text: bool = Field(
        default=False,
        description=(
            "True when the license allows persisting the full body.  "
            "Adapters set this; the ingestion service enforces it."
        ),
    )
    store_summary: bool = Field(
        default=False,
        description=(
            "True when the license allows persisting the short snippet."
        ),
    )
