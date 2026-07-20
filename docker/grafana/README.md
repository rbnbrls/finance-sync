# Grafana Dashboards — finance-sync

This directory contains Grafana dashboard definitions for monitoring the
finance-sync platform.  Dashboards are **provisioned automatically** when
Grafana starts with the supplied provisioning config.

---

## Quick start (docker compose)

```bash
cd /path/to/finance-sync

# Ensure required env vars are set (see .env.example or docker-compose.yml)
export POSTGRES_PASSWORD=your_password
export SECRET_KEY=your_secret_key_min_16_chars

# Start the full stack (postgres, redis, app, worker, prometheus, grafana)
docker compose up -d

# Grafana is now at http://localhost:3000
#   Default login: admin / admin (override via GRAFANA_ADMIN_USER / GRAFANA_ADMIN_PASSWORD)
#
# Prometheus is at http://localhost:9091

```

---

## Dashboards

### 1. Sync Health (`sync-health.json`)
Uid: `finance-sync-sync-health`

Monitors data ingestion pipelines.

| Panel | Metric source | Status |
|-------|--------------|--------|
| Sync runs (24h) | `sync_runs_total` counter | Available now |
| Sync failures (24h) | `sync_runs_total{status="failed"}` | Available now |
| Success rate (24h) | ratio of completed / total | Available now |
| Sync runs by status | `increase(sync_runs_total[24h])` | Available now |
| Sync runs per provider | `increase(sync_runs_total[24h])` by provider | Available now |
| Transactions ingested | `increase(transactions_ingested_total[5m])` | Available now |
| Sync runs over time | `increase(sync_runs_total[5m])` | Available now |
| Scrape health | `up` job freshness | Available now |
| Outbox queue depth | `outbox_messages_pending_total` | **Needs metric** |
| Sync duration | `sync_run_duration_seconds` | **Needs metric** |

### 2. Portfolio (`portfolio.json`)
Uid: `finance-sync-portfolio`

Financial portfolio and net-worth visualisation.

> **Note:** Most portfolio panels query metrics that are **not yet exposed**
> by the application code.  These panels are pre-configured and will light
> up once the corresponding Prometheus gauges are added.  See
> [Adding missing metrics](#adding-missing-metrics) below.

| Panel | Required metric | Status |
|-------|----------------|--------|
| Net worth | `finance_net_worth_eur` | **Needs metric** |
| Portfolio value | `finance_portfolio_value_eur` | **Needs metric** |
| Account count | `finance_account_count` | **Needs metric** |
| Net worth over time | `finance_net_worth_history_eur` | **Needs metric** |
| Portfolio by account | `finance_account_value_eur` | **Needs metric** |
| Allocation by type | `finance_allocation_by_type_eur` | **Needs metric** |
| Top holdings | `finance_top_holdings` | **Needs metric** |
| Account balance trends | `finance_account_balance_eur` | **Needs metric** |

### 3. System (`system.json`)
Uid: `finance-sync-system`

Infrastructure and application performance monitoring.

| Panel | Metric source | Status |
|-------|--------------|--------|
| Request rate | `rate(http_requests_total[5m])` | Available now |
| Latency (p50/p95/p99) | `http_request_duration_seconds_bucket` histogram | Available now |
| Error rate | 5xx / total ratio | Available now |
| Avg response size | `http_response_size_bytes` histogram | Available now |
| Requests by path | `topk rate(http_requests_total)` | Available now |
| DB connection pool | `db_pool_*` gauges | Available now |
| DB pool utilisation | gauge ratio | Available now |
| Redis cache hit ratio | `redis_hits_total` / `redis_misses_total` | **Needs metric** |
| Redis ops rate | `redis_commands_total` | **Needs metric** |
| Worker job durations | `worker_job_duration_seconds` | **Needs metric** |
| Worker job success rate | `worker_job_success_rate` | **Needs metric** |
| Worker health | `up{job=~".*worker.*"}` | Available now |
| Uptime | `process_start_time_seconds` | Available now |

---

## Import into an existing Grafana

If you already run Grafana (e.g. as a sidecar, or a central instance):

1. Copy the dashboard JSON files:
   ```bash
   scp docker/grafana/dashboards/*.json your-server:/path/to/grafana/dashboards/
   ```

2. Add a Prometheus datasource pointing to your finance-sync `/metrics` endpoint:
   - URL: `http://finance-sync-app:8000` (or wherever the app runs)
   - Scrape interval: `15s`

3. Import each dashboard:
   - **UI method:** Grafana → Dashboards → New → Import → paste JSON
   - **API method:**
     ```bash
     curl -X POST http://admin:admin@localhost:3000/api/dashboards/db \
       -H "Content-Type: application/json" \
       -d @sync-health.json
     ```

4. (Optional) Use the `env` template variable to switch between
   dev / staging / prod environments if you run multiple stacks.

---

## Adding missing metrics

Several dashboard panels reference metrics that are not yet emitted by the
application.  Here is where to add them:

### Sync-specific metrics (add to `src/finance_sync/sync/orchestrator.py` or `sync/sync_run.py`)

```python
from prometheus_client import Gauge, Histogram

# Outbox queue depth — gauge updated after each outbox poll
outbox_pending_messages = Gauge(
    "outbox_messages_pending_total",
    "Number of pending outbox messages",
)

# Sync run duration — observe when a sync run completes
sync_run_duration = Histogram(
    "sync_run_duration_seconds",
    "Duration of individual sync runs",
    labelnames=["provider"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600),
)
```

### Finance value gauges (add to a periodic job in `worker/jobs.py`)

```python
from prometheus_client import Gauge

net_worth_gauge = Gauge("finance_net_worth_eur", "Net worth in EUR")
portfolio_value_gauge = Gauge("finance_portfolio_value_eur", "Portfolio value in EUR")
account_count_gauge = Gauge("finance_account_count", "Number of tracked accounts")

# Per-account gauges (use labels)
account_value = Gauge("finance_account_value_eur", "Portfolio value per account", ["account"])
allocation_by_type = Gauge("finance_allocation_by_type_eur", "Allocation by security type", ["type"])
account_balance = Gauge("finance_account_balance_eur", "Account balance", ["account"])
```

### Worker metrics (add to `src/finance_sync/worker/monitoring.py`)

```python
from prometheus_client import Gauge

worker_job_duration_gauge = Gauge(
    "worker_job_duration_seconds",
    "Worker job duration in seconds",
    labelnames=["job_id"],
)
worker_job_success_rate_gauge = Gauge(
    "worker_job_success_rate",
    "Worker job success rate (0-1)",
    labelnames=["job_id"],
)
```

### Redis metrics (add a Prometheus `redis_exporter` sidecar to docker-compose)

```yaml
redis_exporter:
  image: oliver006/redis_exporter:latest
  container_name: finance-sync-redis-exporter
  environment:
    REDIS_ADDR: redis://redis:6379
  ports:
    - "9121:9121"
  depends_on:
    redis:
      condition: service_healthy
```

Then add a Prometheus scrape target for `redis_exporter:9121` in `docker/prometheus.yml`.

---

## Template variables

All three dashboards ship with:

| Variable | Description |
|----------|-------------|
| `datasource` | Prometheus datasource selector |
| `env` | Environment filter (dev / staging / prod) |
| `path` (system only) | Filter by request path |

---

## Version history

| Date | Change |
|------|--------|
| 2026-07-21 | Initial dashboards for Phase 5.3: sync-health, portfolio, system |
