# Capacity limits

This report measures synthetic profiles of 100, 1,000 and 10,000 holdings with
representative transaction counts. It contains read latency, query count,
sync duration, memory, outbox lag, two concurrent workers and a rate-limited
connector flag; it contains no balances, prices or transaction values.

Generate the artifact with:

```bash
uv run python scripts/capacity_limit_report.py
```

The soft limit is 10,000 holdings, 2 concurrent sync workers and 50 pending
outbox messages. The hard limit is 20,000 holdings, 4 workers and 500 pending
messages. The recommended deployment is two API workers, two sync workers,
PostgreSQL 16 with pooled connections and Redis 7 for locks/rate limits.
Re-run the report in staging after infrastructure changes and compare the
artifact with the previous checked-in evidence for the same environment.
