# Holding-relevance notifications — opt-in settings

This document describes the **notification layer** of the holding
feed (story `backlog/plus-relevant-nieuws-en-events.md`, kanban task
t_55d13600): how to opt in, what scoping is available, how
deduplication works, and what is (and is never) shown on the
lockscreen.

## Guarantees

1. **Opt-in only.**  Notifications are **off by default**.  Nothing is
   sent until a user creates a preference row with `enabled: true`.
2. **Deduplicated per story.**  A cluster is a story; notifications are
   logged once per `(user, cluster, event_type)` in
   `relevance_notification_log` (unique constraint).  Three syndicated
   articles about one earnings call → **one** notification.  Re-running
   the dispatch (or a second build tick) never double-sends.
3. **Lockscreen-safe by default.**  The default payload contains only:
   * `event_type` (e.g. `earnings`)
   * `headline` (truncated to 200 chars)
   * `event_date` (ISO-8601)
   * `source_url`
   * `lockscreen_safe: true`

   It never contains position sizes, quantities, market values or any
   financial figure.
4. **Explicit detailed preview.**  Setting `detailed_preview: true`
   adds `security_ticker` and `security_name` to the payload — still
   **never** financial values.  This is an explicit opt-in; the safe
   default is `false`.
5. **Tenant isolation.**  Preferences, dispatch and the notification log
   are all tenant-scoped.  A user of tenant B can never trigger or read
   notifications for tenant A's clusters — a cross-tenant cluster id
   returns `not_found`/empty, never a leak.

## Preference fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | `false` | Master opt-in switch |
| `lockscreen_safe` | bool | `true` | Never leak position size/financial value on the lockscreen |
| `detailed_preview` | bool | `false` | Explicit opt-in to include security ticker/name in the payload |
| `event_types` | list[str] \| null | `null` (all) | Allowed event types (`earnings`, `dividend`, `agm`, `split`, `merger`, `acquisition`, `filing`, `news`, `interest`, `currency`) |
| `security_id` | uuid \| null | `null` (all) | Per-security scope: only notify for clusters of this security |
| `account_id` | uuid \| null | `null` (all) | Per-account scope: only notify for clusters touching this account |

A preference row is unique per `(tenant_id, user_id)` — one row per
user per household, updated in place.

## REST API

* `GET  /api/v1/holding-relevance/notifications/preferences`
  — returns the signed-in user's settings (or the safe defaults).
* `PUT  /api/v1/holding-relevance/notifications/preferences`
  — create/update.  Only fields provided are changed.  Requires the
  `market-intelligence:write` permission.
* `POST /api/v1/holding-relevance/notifications/{cluster_id}/send`
  — fire one deduplicated, lockscreen-safe notification for a cluster
  (respects opt-in, scoping and dedupe; useful for manual/on-demand
  sends from the UI).

Example:

```bash
curl -X PUT "$API/v1/holding-relevance/notifications/preferences" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "enabled": true,
    "detailed_preview": true,
    "event_types": ["earnings", "dividend"],
    "security_id": "<security-uuid>"
  }'
```

## MCP tools

| Tool | Purpose |
|---|---|
| `get_holding_notification_preferences` | Read the principal's settings |
| `set_holding_notification_preferences` | Create/update (same fields as the REST body) |

## How notifications are emitted

The worker job `holding_relevance_build` runs on its own cadence
(`WORKER_JOB_HOLDING_RELEVANCE_INTERVAL_MINUTES`, default 60).  After
each tenant's feed build it calls
`HoldingRelevanceService.dispatch_new_cluster_notifications(tenant_id)`,
which:

1. loads every **enabled** preference row of the tenant;
2. for each cluster, applies the preference's `event_types`,
   `security_id` and `account_id` scoping;
3. logs one `relevance_notification_log` row per eligible
   `(user, cluster, event_type)` — the unique constraint makes the
   dispatch idempotent even if a tick is missed or two workers race.

The payload stored in the log is the same lockscreen-safe snapshot the
UI/agent would render; delivery to an actual push channel is a consumer
concern (the log row is the durable, deduplicated notification event).

## Security notes

* Payloads are built by `_build_notification_payload` and never include
  `quantity`, `market_value`, `position` or any financial value.
* Cross-tenant cluster ids in `notify_eligible` / dispatch resolve to
  `not_found` / no-op — existence is never leaked.
* Preference `user_id` is the auth principal id (JWT user or API-key
  id), stable per machine principal.
* The notification log stores no secrets; upstream source URLs are the
  same canonical, sanitised links served by the feed.

## Operations

| Setting | Purpose |
|---|---|
| `WORKER_JOB_HOLDING_RELEVANCE_ENABLED` | Master switch for the build+dispatch job (default `true`) |
| `WORKER_JOB_HOLDING_RELEVANCE_INTERVAL_MINUTES` | Cadence (default 60) |

The notification log grows by at most one row per (user, cluster,
event-type); it is safe to prune rows older than a retention window if
desired (nothing in the pipeline requires historical log rows).
