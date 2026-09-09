# Release 19 autoscaling loadtest baseline

The release gate runs `scripts/loadtest_autoscaling.py` against
`config/loadtest-profiles.json`. The simulation is deterministic and uses only
request counts and timings: it never contacts a provider, database, queue, or
uses financial values.

## Profiles and measurements

Four profiles cover API reads, sync runs, retries and outbox consumers. Each
report records p95 latency, error rate, maximum queue depth, maximum database
connections and worker count. It also records provider-rate compliance,
backpressure compliance, overload action and duplicate writes.

The baseline uses two API workers and two sync/outbox workers. Sync workers may
scale from two to four while queue depth remains below 500. At the hard queue
limit new work is rejected with `Retry-After`; provider traffic is capped at
20 requests/second per tenant. A synthetic overload therefore fails in a
controlled way and records zero duplicate writes.

Generate the artifact locally:

```sh
uv run python scripts/loadtest_autoscaling.py \
  --config config/loadtest-profiles.json \
  --artifact loadtest-autoscaling.json
```

The artifact is safe to publish: it contains no account identifiers, provider
payloads, prices, balances, or other financial data.
