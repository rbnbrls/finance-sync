# Autoscaling and backpressure

The policy in `config/autoscaling-policy.json` keeps sync concurrency between
1 and 4, API traffic below 100 requests/second, queue depth below 500 and
database connections below 40. At queue depth 50 workers scale up and new
syncs slow down; at 500 new syncs receive a retry-after response. Exhausted
database connections return service-busy without a retry storm. Provider 429s
always retain provider-specific backoff per tenant.

The policy is evaluated by `scripts/autoscaling_policy.py` and tested against
synthetic burst scenarios. Alerts are raised at queue depth 50, 36 database
connections and 4 concurrent sync workers. Decisions contain no financial
values or tenant identifiers.
