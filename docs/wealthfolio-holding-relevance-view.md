# Wealthfolio companion view — holding feed & event calendar

This document describes the **Wealthfolio add-on / companion view** for
holding-relevant news and corporate events (story
`backlog/plus-relevant-nieuws-en-events.md`, kanban task t_76f1de63).

The companion view renders the same holding feed and event calendar as
the finance-sync control panel, **without ever touching the Wealthfolio
SQLite database**.  All data comes from the finance-sync REST API
(`/api/v1/holding-relevance/*`), which is tenant-scoped and filtered by
the signed-in user's permissions.

## Design rules

1. **Read-only from finance-sync, never from Wealthfolio.**  The view
   consumes `GET /api/v1/holding-relevance/feed` and
   `GET /api/v1/holding-relevance/calendar`.  It opens no SQLite file,
   holds no lock, and cannot corrupt or block the Wealthfolio WAL
   database.
2. **Tenant + user scoped.**  Every request carries the user's JWT
   (`Authorization: Bearer …`).  The API applies the same
   tenant/household isolation as everywhere else: a cross-tenant
   security or account filter returns an empty feed, never a leak.
3. **Escaped rendering.**  All headlines, security names, source URLs
   and provider labels from the API are HTML-escaped before they are
   interpolated into the DOM (no XSS via syndicated titles).
4. **Lockscreen-safe.**  The companion view never shows position sizes
   or financial values.  Only the security, event type, headline and
   event date are rendered.
5. **Graceful degradation.**  A stale or missing source never breaks the
   page: clusters carry an `is_stale` badge, and a failing API call
   renders a friendly inline error instead of a blank body.

## Where the view lives

| Piece | Location |
|---|---|
| Server-rendered page | `GET /holdings-relevance` (GUI router) |
| Template | `src/finance_sync/templates/holding_relevance.html` |
| Control-panel section | Sidebar → "Holdingnieuws" in `dashboard.html` |
| API consumed | `/api/v1/holding-relevance/feed`, `/calendar`, `/clusters/{id}/ack` |
| Service behind the API | `src/finance_sync/services/holding_relevance.py` |
| This document | `docs/wealthfolio-holding-relevance-view.md` |

## Using the companion view

### Standalone

1. Sign in at the finance-sync control panel (`/login`).
2. Open `/holdings-relevance` in a browser.  The page shows the holding
   feed (ranked clusters with filters: security, account, event type,
   date, unread/acknowledged state) and the event calendar (upcoming
   corporate events sorted by date).
3. Use the filter bar and the per-cluster "Markeer gelezen/ongelezen"
   buttons.  Ack changes round-trip through
   `POST /api/v1/holding-relevance/clusters/{id}/ack` and are reflected
   immediately after a reload.

### Embedded in the Wealthfolio UI

The page is designed to be embedded as an **iframe** in the Wealthfolio
desktop/PWA without cross-origin headaches:

```html
<iframe
    src="https://<finance-sync-host>/holdings-relevance"
    style="width:100%; height:70vh; border:0; border-radius:10px;"
    title="Holdingnieuws &amp; events"
    sandbox="allow-scripts allow-same-origin allow-forms"
></iframe>
```

Because the iframe is same-origin with finance-sync (or served from the
finance-sync host), the stored `fs_token` in `localStorage` is used
automatically.  If the token is missing/expired the iframe redirects to
`/login?next=/holdings-relevance`.

To embed inside a **separate** origin (e.g. a Wealthfolio instance on
another host), add an iframe URL that includes the token as a fragment:

```html
<iframe
    src="https://<finance-sync-host>/holdings-relevance#token=<JWT>"
    ...
></iframe>
```

The template reads `location.hash` and, when a `token=` fragment is
present, uses it instead of `localStorage` (see
`holding_relevance.html`).  The token is never placed in a query string
(which would leak into logs/referrers).

## Filter contract (mirrors the API)

| Filter | API param | Notes |
|---|---|---|
| Security | `security_id` | Canonical security UUID; cross-tenant → empty |
| Account | `account_id` | Cross-tenant → empty |
| Item type | `item_type` | `earnings`, `dividend`, `agm`, `split`, `merger`, `acquisition`, `filing`, `news`, `interest`, `currency` |
| Date range | `date_from` / `date_to` | ISO 8601 datetimes |
| Unread | `unread_only=true` | Only clusters the user has not acknowledged |
| Acknowledged | `acknowledged=true` | Only clusters the user acknowledged |

Every feed item carries: `id`/`cluster_id`, `security_id`,
`security_ticker`, `security_name`, `event_type`, `event_date`,
`headline`, `score`, `match_reason`, `confidence`, `is_stale`,
`source_count`, `cluster_reason`, `earliest_published_at`, `item_ids`,
`best_source_url`, `acknowledged` and a `sources[]` list with `url`,
`published_at`, `fetched_at`, `freshness` per source.

## Security notes

- The page renders **no credentials** and **no position sizes / market
  values**.  The feed deliberately shows only the security, event type,
  headline, date and source links.
- Headlines from upstream providers are treated as untrusted data and
  escaped.  Source URLs are escaped and opened with
  `rel="noopener noreferrer"`.
- The backend already treats filter values as data (parameterised
  queries); the companion view passes them through unchanged, so
  injection payloads match nothing.
- The `#token=` fragment embedding is **optional**; when the fragment is
  absent the page falls back to the normal `localStorage` session.  No
  token is ever written into the URL path or query string by the view
  itself.

## Runbook: verify feed + calendar render from API data

Prerequisite: a running finance-sync instance with holding-relevance
data (the market-intelligence source layer + the relevance build have
run at least once, e.g. via the `intel_refresh` worker job).

```bash
# 1. Sign in to get a JWT (or reuse an existing session token).
BASE="https://<finance-sync-host>"
TOKEN="$(curl -s -X POST "$BASE/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"***"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')"
AUTH="Authorization: Bearer $TOKEN"

# 2. Feed renders non-empty, ranked, with source URLs + freshness.
curl -s -H "$AUTH" "$BASE/api/v1/holding-relevance/feed?limit=10" \
  | python3 -c '
import sys, json
body = json.load(sys.stdin)
print("total:", body["total"])
for it in body["items"]:
    print(f"- [{it[\"event_type\"]}] {it[\"headline\"]} "
          f"(ticker={it[\"security_ticker\"]}, ack={it[\"acknowledged\"]}, "
          f"stale={it[\"is_stale\"]}, score={it[\"score\"]})")
    for s in it["sources"]:
        print(f"    src: {s[\"url\"]} freshness={s[\"freshness\"]}")'

# 3. Calendar returns upcoming/past event clusters by date.
curl -s -H "$AUTH" "$BASE/api/v1/holding-relevance/calendar?limit=20" \
  | python3 -c '
import sys, json
body = json.load(sys.stdin)
print("calendar events:", body["total"])
for ev in body["events"]:
    print(f"- {ev[\"event_date\"]} [{ev[\"event_type\"]}] "
          f"{ev[\"security_ticker\"]}: {ev[\"headline\"]}")'

# 4. Ack round-trip: mark the first cluster read, then re-read.
CLUSTER="$(curl -s -H "$AUTH" "$BASE/api/v1/holding-relevance/feed?limit=1" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["items"][0]["cluster_id"])')"
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"acknowledged": true}' \
  "$BASE/api/v1/holding-relevance/clusters/$CLUSTER/ack"
curl -s -H "$AUTH" "$BASE/api/v1/holding-relevance/feed?acknowledged=true&limit=5" \
  | python3 -c 'import sys,json; b=json.load(sys.stdin); print("acked total:", b["total"])'

# 5. The companion page itself is served.
curl -s -o /dev/null -w "companion page HTTP %{http_code}\n" "$BASE/holdings-relevance"

# 6. Tenant isolation smoke check: a foreign security id returns empty.
curl -s -H "$AUTH" "$BASE/api/v1/holding-relevance/feed?security_id=00000000-0000-0000-0000-000000000000" \
  | python3 -c 'import sys,json; assert json.load(sys.stdin)["total"] == 0; print("isolation OK")'
```

Expected result: the feed lists ranked clusters with source links and
freshness, the calendar lists dated events, ack round-trips are visible
in later reads, the companion page returns HTTP 200, and a foreign
security id returns an empty feed (never an error, never a leak).

## References

- `docs/market-intelligence.md` — the source layer that produces the
  observations.
- `src/finance_sync/services/holding_relevance.py` — matching, clustering
  and ranking logic.
- `src/finance_sync/api/v1/holding_relevance.py` — REST contract.
- `src/finance_sync/mcp/server.py` — MCP tools (`get_holding_feed`,
  `get_holding_calendar`, `acknowledge_holding_cluster`).
