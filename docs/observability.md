# Observability — metrics & alerting

## Release 16 SLOs

The canonical SLO and alert contract is `config/slo-alerts.json`. It covers
sync-success rate (99% over 24 hours), sync-duration p95 (under 15 minutes),
outbox lag (under 50 messages for 10 minutes) and worker-failure rate (under
1% per hour). Metrics use only operational labels such as provider, queue,
job and status; tenant IDs, account identifiers, credentials and financial
values are prohibited.

Alerts carry `warning` or `critical` severity, a runbook link and the
`maintenance-window` suppression. The synthetic alert test validates every
threshold and confirms that planned maintenance suppresses notifications.

Ownership is `finance-platform-oncall`; escalation starts with the on-call
operator, then the platform owner for infrastructure failures and the
connector owner for provider-specific failures. Dashboards are provisioned
under the `sync-health` dashboard.

finance-sync ships Prometheus + Grafana in the compose stack
(`docker-compose.yml`) with provisioned dashboards **and** alert rules.
This document is the reference for the alert inventory, notification
channels, and how to silence / tune alerts.

---

## 1. Stack layout

| Component | Container | Endpoint | Notes |
|-----------|-----------|----------|-------|
| Prometheus | `finance-sync-prometheus` | `:9091` (host) | Scrapes app + worker every 15s |
| Grafana | `finance-sync-grafana` | `:3000` | Dashboards, alert rules, notifications |
| App metrics | `app` | `app:8000/metrics` | HTTP, DB pool, sync, ingestion, export counters |
| Worker metrics | `worker` | `worker:9090/metrics` | Outbox backlog, enrichment staleness, job gauges |

Scrape config: `docker/prometheus.yml` (jobs `finance-sync-app` and
`finance-sync-worker`).

Grafana provisioning (all file-based, auto-loaded at startup):

- `docker/grafana/provisioning/datasources/prometheus.yaml`
- `docker/grafana/provisioning/dashboards/dashboards.yaml` → `docker/grafana/dashboards/*.json`
- `docker/grafana/provisioning/alerting/alerting.yaml` (contact points, policies, mute timings)
- `docker/grafana/provisioning/alerting/finance-sync.rules.yaml` (alert rules)

---

## 2. Alert inventory

All rules live in the **Finance Sync** folder, evaluate every 1 minute,
and are routed to the **Finance Sync** contact point.  Rule UIDs are
stable (file-provisioned) and appear in the UI at
`/alerting/list?search=Finance%20Sync`.

| Rule (uid) | Condition | Threshold | Severity | Channel |
|------------|-----------|-----------|----------|---------|
| `finance-sync-failed-sync-runs` | `increase(sync_runs_total{status="failed"}[15m])` | `> 0` for 5m | critical | webhook + email |
| `finance-sync-stale-enrichment` | `(time() - enrichment_last_success_timestamp)` or never-set | `> 24h` for 30m | warning | webhook + email |
| `finance-sync-outbox-backlog` | `outbox_messages_pending_total` | `> 50` for 10m | warning | webhook + email |
| `finance-sync-export-failures` | `increase(export_runs_total{status="failed"}[1h])` | `> 0` for 5m | critical | webhook + email |
| `finance-sync-worker-down` | `up{job="finance-sync-worker"}` | `== 0` for 2m | critical | webhook + email |
| `finance-sync-app-down` | `up{job="finance-sync-app"}` | `== 0` for 2m | critical | webhook + email |
| `finance-sync-dr-rpo-breach` | `dr_sla_last_usable_backup_age_seconds` | `> 900` (15m) for 15m | critical | webhook + email |
| `finance-sync-dr-rto-breach` | `dr_sla_replay_lag_seconds` | `> 1800` (30m) for 15m | critical | webhook + email |

Each dashboard panel that can trip an alert carries a "View alert" link
to the corresponding rule.

---

## 3. Notification channels

Alerts are routed to the **Finance Sync** contact point
(`docker/grafana/provisioning/alerting/alerting.yaml`), which has two
receivers:

1. **Webhook** — URL from `GRAFANA_ALERT_WEBHOOK_URL`
   (Slack / Discord / Telegram / ntfy / your own endpoint).
2. **Email** — addresses from `GRAFANA_ALERT_EMAILS`
   (comma-separated). Requires Grafana SMTP to be configured
   (`GF_SMTP_*` env vars in docker-compose, currently commented out).

Defaults in `docker-compose.yml` keep provisioning safe:

```yaml
GRAFANA_ALERT_WEBHOOK_URL: ${GRAFANA_ALERT_WEBHOOK_URL:-http://localhost:3000}
GRAFANA_ALERT_EMAILS:     ${GRAFANA_ALERT_EMAILS:-admin@localhost}
```

> With the defaults, alerts fire and are **visible in the Grafana UI**,
> but the webhook points at a no-op placeholder and email has no SMTP.
> To get notified, set the real values in `.env`:
>
> ```bash
> # .env
> GRAFANA_ALERT_WEBHOOK_URL=https://example.com/webhook/slack
> GRAFANA_ALERT_EMAILS=ops@example.com
> SMTP_PASSWORD=secret            # used by the commented GF_SMTP_* block
> ```
>
> Then `docker compose up -d grafana`.

### Testing a channel

Grafana UI → **Alerting → Contact points → Finance Sync → Test**.
It sends a test alert through every receiver in the contact point.

---

## 4. Silencing alerts

Three mechanisms, in increasing order of scope:

1. **Silence button** (short-term): Alerting → **Silences → New
   silence**.  Matches by label (`alertname`, `team`, ...) and duration.
   Persists in Grafana (survives restarts).
2. **Mute timing** (recurring, provisioned): the
   `maintenance-window` mute timing in `alerting.yaml` currently
   suppresses notifications on weekend nights as an example.  Add your
   own intervals there (file-provisioned, survives restarts).
3. **Pause a rule** (Alerting → Alert rules → rule → **Pause**): stops
   evaluation entirely.  Note: pausing in the UI is stored in the
   Grafana DB, not in the provisioning file, so it is lost on a fresh
   `grafana_data` volume.

To permanently disable a provisioned rule, delete it from
`finance-sync.rules.yaml` (or comment it out) and restart Grafana —
provisioning is authoritative.

---

## 5. Tuning thresholds

All thresholds are plain values in
`docker/grafana/provisioning/alerting/finance-sync.rules.yaml`:

| Rule | Where to tune |
|------|---------------|
| Outbox backlog | `params: [50]` in the threshold node; `for: 10m` |
| Stale enrichment | `> 86400` (seconds); `for: 30m` |
| Failed syncs / exports / down | thresholds are `> 0` — tune `for:` instead |

After editing, restart Grafana to re-provision:

```bash
docker compose restart grafana
```

---

## 6. Metric reference

| Metric | Type | Labels | Emitted by |
|--------|------|--------|------------|
| `sync_runs_total` | counter | `provider`, `status` | orchestrator (`run_sync`, cards pipeline) |
| `transactions_ingested_total` | counter | `provider` | orchestrator |
| `sync_run_duration_seconds` | gauge | `provider` | orchestrator |
| `outbox_messages_pending_total` | gauge | — | outbox publisher (true backlog count) |
| `enrichment_last_success_timestamp` | gauge | — | `enrich_prices_job` on success |
| `export_runs_total` | counter | `exporter`, `status` | Actual Budget + Wealthfolio exporters |
| `worker_job_duration_seconds` | gauge | `job_id` | JobMonitor |
| `worker_job_success_rate` | gauge | `job_id` | JobMonitor |
| `http_requests_total` / `http_request_duration_seconds` / `http_*_size_bytes` | counter / histogram | `method`, `path`, `status` | app middleware |
| `db_pool_*` | gauge | — | app middleware |
| `dr_sla_last_usable_backup_age_seconds` | gauge | — | `scripts/dr_sla_monitoring.py` (CI artifact + metrics) |
| `dr_sla_replay_lag_seconds` | gauge | — | `scripts/dr_sla_monitoring.py` (CI artifact + metrics) |
| `dr_sla_restore_duration_seconds` | gauge | — | `scripts/dr_sla_monitoring.py` (CI artifact + metrics) |

All of these are served from the **app** (`app:8000/metrics`) or the
**worker** (`worker:9090/metrics`) — see the table in §1.

---

## 7. Troubleshooting

- **Alert doesn't fire** — check the rule's query in Grafana
  (Alerting → Alert rules → rule → "Query" tab): if it returns *No
  data*, the metric isn't being emitted.  Verify the app/worker scrape
  targets in Prometheus (`:9091/targets`).
- **`finance-sync-worker-down` fires spuriously** — the worker's
  `/metrics` route requires the worker to run the outbox/enrichment
  modules (they register the gauges).  If the worker container is up,
  check `curl worker:9090/metrics` inside the compose network.
- **Dashboard shows "No data" for outbox/sync panels** — those metrics
  only appear after the first sync run / outbox poll.  Give the stack
  a few minutes.
