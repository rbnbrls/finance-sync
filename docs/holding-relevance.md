# Holding relevance — news & corporate events for your holdings

This document describes the **holding-relevance layer** (story
`backlog/plus-relevant-nieuws-en-events.md`): how finance-sync links
market-intelligence observations to your current (and recently sold)
holdings, clusters syndicated coverage into one story, ranks the feed,
and exposes it through the REST API, MCP tools, the control panel and a
Wealthfolio companion view.

## Principles

1. **Deterministic facts only.**  The security match, holding status,
   dates and source references are computed by finance-sync from
   canonical rows — never invented by an LLM.  A Hermes *explanation*
   (optional, off by default) can only cite these rows.
2. **No spam from syndication.**  Three articles about the same event
   are **one story** with three source links; notifications fire once
   per story, never per article.
3. **Tenant + user scoped.**  Every row belongs to one tenant; acks and
   corrections are additionally per-user, so one household's feedback
   never affects another.
4. **Lockscreen-safe by default.**  Notifications and companion views
   never leak position sizes or financial values unless the user
   explicitly opts into detailed previews.
5. **Graceful degradation.**  A stale or missing source never breaks a
   read: clusters carry an `is_stale` flag, missing source links simply
   render without the link, and the score stays deterministic.

## Pipeline

```
market-intelligence observations (intel layer, security-resolved)
      ↓
HoldingRelevanceService.build_feed(tenant)      — match holdings
      ↓  match_reason: canonical_security / recently_sold / currency_interest
HoldingRelevanceService._recluster(tenant)      — cluster stories
      ↓  story_key = security + event_type + event_date (or fingerprint)
relevance_clusters (+ source edges)
      ↓
feed / calendar DTOs  — REST + MCP + control panel + companion view
      ↓
dispatch_new_cluster_notifications(tenant)      — opt-in, deduped, safe
```

### Matching

An observation is relevant when:

* its resolved canonical `security_id` is currently held (quantity > 0
  in the latest snapshot per account+security) — `canonical_security`,
  confidence 1.0, carrying the normalised position weight; or
* the security was sold within the last 180 days — `recently_sold`,
  confidence 0.8; or
* the observation is an **interest or currency event** and the tenant
  has matching cash accounts (bunq savings/checking/cash) —
  `currency_interest`, confidence 0.6.  Plain cash news is **not**
  matched.

Generic ticker/name matches without a resolved canonical security are
never shown as holding news.  A per-user **correction flow**
(`POST /api/v1/holding-relevance/corrections`, MCP
`correct_holding_item`) suppresses false positives for the correcting
user only, never deletes the underlying observation, and keeps similar
future items out (re-match prevention via title fingerprint).

### Clustering

Clusters are deterministic and keyed on `(security_id, event_type,
event_date)`:

| Reason | When | Example |
|---|---|---|
| `exact_event` | same security + event type + event date (day) | two wire stories about the same earnings call |
| `title_duplicate` | event dates within 3 days **and** matching title fingerprint | syndicated variants of one story |
| `no_date` | date-less items with matching title fingerprint | two undated press notes of the same story |

Distinct events always stay separate: different quarters, an ex-date
vs. a payment date, or a dividend vs. an earnings story never merge.
A cluster keeps **every** source link (`source_count`, `sources[]` with
URL, published/fetched timestamps and per-source freshness) and the
earliest published timestamp.

### Ranking / scoring

The deterministic `score` (0..1) is:

```
score = holding_weight × event_proximity × recency × source_reliability
```

* **holding_weight** — the security's latest market value divided by
  the tenant's total (NULL for recently-sold/cash → neutral 0.5).
* **event_proximity** — 1.0 for today, decaying smoothly with distance
  (past and future); nearer events rank higher.
* **recency** — newer source items rank higher (based on the cluster's
  earliest published timestamp).
* **source_reliability** — `sec` 1.0, `sec_press` 0.9, `openbb` 0.7,
  unknown providers 0.5.

Same input → same score and same order (no wall-clock or random state).

## Supported events

| Event type | Source fields used | Notes |
|---|---|---|
| `earnings` | `earnings_date` / `event_date` | SEC 8-K Item 2.02, OpenBB |
| `dividend` | `ex_date`, `record_date`, `payment_date` | distinct dates are distinct stories |
| `agm` | `meeting_date` | shareholder meetings |
| `split` | `split_date` | |
| `merger` / `acquisition` | headline + kind | |
| `filing` | headline / kind (8-K etc.) | regulatory filings |
| `news` | published date | plain company news |
| `interest` | `event_type` fact / kind | cash-portfolio relevance only |
| `currency` | `event_type` fact / kind / `currency_pair` | cash-portfolio relevance only |

## Data sources

The source layer is documented in full in
[docs/market-intelligence.md](market-intelligence.md).  In short:

| Provider | What it feeds | Licence class |
|---|---|---|
| SEC EDGAR (`sec`) | 8-K current reports → corporate events + earnings | public domain |
| SEC press (`sec_press`) | official SEC announcements (news) | public domain |
| OpenBB (`openbb`) | news headlines + snippets, optional earnings | per OpenBB terms |

finance-sync persists metadata, a short snippet and structured facts
plus a canonical link — never full copyrighted article text.  A
provider that is down, rate-limited or unconfigured reports
`unavailable`; stored observations older than the freshness threshold
are served with `freshness: stale` / `is_stale: true` instead of being
deleted.

## Per-market limitations

| Market / source | Limitation |
|---|---|
| US equities (SEC EDGAR) | Corporate events limited to the 8-K item list in `intel/adapters/sec.py`; coverage = US-listed companies with a CIK |
| US (SEC press) | Official SEC announcements only; **not** company-specific news, no ticker scoping |
| OpenBB | Coverage depends on the configured backend + key entitlements; `news` + optional `earnings` only |
| EU / NL equities | No dedicated EU corporate-action feed shipped yet — EU events surface only when a configured provider (e.g. OpenBB) delivers them; dividend/AGM coverage is therefore sparser than US |
| Cash (bunq) | Only interest and currency events are relevant; plain cash news is ignored by design |
| Recently sold | 180-day window (`RECENTLY_SOLD_WINDOW`); older positions stop surfacing news |

## Freshness & degradation

* `STALE_AFTER` = 24 h default (per-provider `stale_after` overrides).
* A cluster is `is_stale` when **every** source item is stale; a mix is
  served fresh with per-source staleness flags.
* `include_stale=false` filters all-stale clusters out of the feed.
* Missing/cross-tenant security or account filter values match **no
  rows** (never widen the filter, never 500); injection payloads are
  treated as data.

## Exposed surfaces

| Surface | Where |
|---|---|
| REST feed | `GET /api/v1/holding-relevance/feed` (filters: security, account, item type, date, unread/ack, include_stale, limit/offset) |
| REST calendar | `GET /api/v1/holding-relevance/calendar` |
| REST ack | `POST /api/v1/holding-relevance/clusters/{id}/ack` |
| REST corrections | `POST /api/v1/holding-relevance/corrections` |
| REST notification preferences | `GET/PUT /api/v1/holding-relevance/notifications/preferences` |
| MCP | `get_holding_feed`, `get_holding_calendar`, `acknowledge_holding_cluster`, `correct_holding_item`, `get_holding_notification_preferences`, `set_holding_notification_preferences` |
| Control panel | Sidebar → Holdingnieuws (`dashboard.html`) |
| Wealthfolio companion view | `GET /holdings-relevance` — see [docs/wealthfolio-holding-relevance-view.md](wealthfolio-holding-relevance-view.md) |
| Notifications | See [docs/holding-relevance-notifications.md](holding-relevance-notifications.md) |

## Hermes explanations

When `HERMES_EXPLANATION_ENABLED=true`, feed DTOs may carry a
`hermes_explanation` field: a few sentences explaining *why* an item is
relevant, grounded only in deterministic finance-sync facts (security
name/ticker, event type/date, match reason, item IDs, source URL) —
never financial values or position sizes.  With Hermes unavailable or
disabled the field is simply omitted and the deterministic feed stays
fully available.  See [docs/hermes-relevance.md](hermes-relevance.md)
if present, or `src/finance_sync/services/hermes_relevance.py`.

## Operations

The build runs on the worker via `holding_relevance_build` every
`WORKER_JOB_HOLDING_RELEVANCE_INTERVAL_MINUTES` (default 60), gated by
`WORKER_JOB_HOLDING_RELEVANCE_ENABLED`.  Idempotent: re-running is a
no-op except for newly ingested observations; after each build,
opt-in notifications are dispatched for new clusters (deduplicated).
Disable the whole feature with `WORKER_JOB_HOLDING_RELEVANCE_ENABLED=false`.
