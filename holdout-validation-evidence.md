# Holdout-validatie PR #547

Commit: `4993ffca86ace38528e1865ef5c35996880b2772`
Repository: `rbnbrls/finance-sync`

## Scope en veiligheid

Alle tests gebruiken uitsluitend synthetische tenant-labels, dummy markers en getelde load-profielen. Er zijn geen providercredentials, database- of queue-services en geen financiële waarden gebruikt. De numerieke profielen en drempels zijn contract-/simulatie-inputs, geen productiecapaciteitsmeting.

## Reproduceerbare commando's

Werkdirectory: `/home/hermes/.hermes/kanban/boards/code/workspaces/t_88239c50/finance-sync`

- `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -n auto -m "not integration and not e2e" --ignore=test_holdout_autoscaling.py`
  - PASS: `3639 passed, 8 skipped, 181 warnings in 44.78s` (exit 0).
- `uv run ruff check test_holdout_autoscaling.py && uv run ruff format --check test_holdout_autoscaling.py`
  - PASS: `All checks passed!`; `1 file already formatted` (exit 0).
- `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -q test_holdout_autoscaling.py`
  - FAIL zoals bedoeld: 8 scenario's uitgevoerd, 7 PASS en 1 FAIL (exit 1).
- `APP_ENVIRONMENT=dev DEBUG=false uv run python test_holdout_autoscaling.py`
  - Zelfde onafhankelijke scenario-uitvoer: 7 PASS / 1 FAIL (exit 1).
- `uv run python scripts/loadtest_autoscaling.py --artifact holdout-loadtest-profiles.json`
  - PASS (exit 0); schreef synthetische profielmetingen.
- Policy-bounds probe met `scripts.autoscaling_policy.decide` op baseline, soft queue, hard queue, uitgeputte DB en provider-rate-limit
  - PASS (exit 0); acties hieronder vastgelegd.

## Scenario-verdicts

Bron: `holdout-autoscaling-report.json`; latency is de lokale tijd van de deterministische check, niet netwerk/API-latency.

| # | Scenario | Verdict | Bewijs / meetwaarden |
|---|---|---|---|
| 1 | Gepachte provider-responses en retry-injectie | PASS | 80 requests; p95 28.0 ms; error rate 0.05; queue max 0; DB max 12; workers max 2; provider rate 4.0; rate-limit gerespecteerd; duplicate writes 0; backpressure true. Check-latency 0.015 ms in laatste run. |
| 2 | Tenant-isolatie onder gelijktijdige autoscaling | PASS | Policy `tenant_isolation=true`; 41 `tenant_id`-predicaten in repositories; queue-hard actie `reject_new_syncs`; Retry-After 30 s; duplicate/financiële waarden niet gebruikt. Check-latency 0.116 ms. |
| 3 | Secrets in loadtest-observability | PASS | Geen dummy secret markers gevonden; `financial_values_in_report=false`. Check-latency 0.064 ms. |
| 4 | Retry na time-out met onzekere providerstatus | PASS | Outbox-creatie en publisher-lookup op `idempotency_key`; model heeft `unique=True`; duplicate writes 0. Check-latency 0.117 ms. |
| 5 | Crash en herstel tijdens outbox-publicatie | PASS | Publisher bevat acknowledgement-, idempotency- en processed-paden; events lost 0. Check-latency 0.124 ms. |
| 6 | Rate-limit reset en klokafwijking | PASS | `Retry-After` parsing aanwezig; limiter gebruikt `retry_after`; policy kiest `provider_backoff`; provider rate-limit behouden; tenant isolation true. Check-latency 0.108 ms. |
| 7 | Autoscaling-thrashing en afbouw met actieve leases | **FAIL** | Geen expliciet `hysteresis`, `cooldown` of active-lease `drain` contract in de PR; oscillation test niet uitgevoerd. Check-latency 1.233 ms. Dit is een echte unmet criterion, geen test-harnasfout. |
| 8 | Onvolledige afhankelijkheidsuitval | PASS | DB op max 40 geeft `service_busy` / `database_connections_exhausted`; queue op hard limit 500 geeft `reject_new_syncs` / Retry-After 30 s; geen retry storm-contract geschonden; financiële waarden niet gebruikt. Check-latency 0.003 ms. |

## Synthetische loadprofielen

Uit `holdout-loadtest-profiles.json`:

| Profiel | Requests | p95 (ms) | Error rate | Queue max | DB max | Workers max | Provider rate | Duplicates |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| api_reads | 1000 | 28.0 | 0.00 | 0 | 12 | 2 | 20.0 | 0 |
| sync_runs | 120 | 36.0 | 0.00 | 0 | 20 | 4 | 2.0 | 0 |
| retries | 80 | 28.0 | 0.05 | 0 | 12 | 2 | 4.0 | 0 |
| outbox_consumers | 600 | 36.0 | 0.00 | 0 | 20 | 4 | 10.0 | 0 |

Alle vier profielen rapporteren `backpressure_respected=true` en `provider_rate_limit_respected=true`. Omdat dit count-only simulatie is, bewijst dit niet de live runtime onder meerdere workers/providers/DB/queue.

## Policy-boundaries

De uitvoer van de boundary probe is:

- baseline: `accept`, reason `within_limits`
- queue 50: `scale_workers_and_slow_syncs`, reason `queue_depth_soft_limit`
- queue 500: `reject_new_syncs`, reason `queue_depth_hard_limit`, Retry-After 30 s
- DB 40: `service_busy`, reason `database_connections_exhausted`
- provider limited: `provider_backoff`, reason `provider_rate_limit`

Elke beslissing rapporteerde `tenant_isolation=true`, `financial_values_in_decision=false` en behoud van de provider-rate-limit.

## Veilige baseline en schaaladvies

- Baseline: 2 API-workers en 2 sync/outbox-workers; begin met sync-concurrency 1-2 per worker.
- Schaal sync-workers gecontroleerd van 2 naar maximaal 4 zolang queue depth onder hard limit 500 blijft; alert bij queue 50 en DB 36/40 verbindingen.
- Houd API-verkeer op maximaal 100 requests/s en providerverkeer op maximaal 20 requests/s per tenant volgens de synthetische config.
- Bij queue depth >=500: nieuwe syncs weigeren met Retry-After 30 s; geen agressieve retry-loop.
- Bij DB-uitputting: `service_busy` teruggeven en retries afremmen.
- Bij provider-rate-limit: per tenant backoff respecteren.
- Niet uitrollen op basis van deze holdout alleen: scenario 7 blijft FAIL en er is geen live multi-worker/provider/DB/queue-meting. Voor productie is eerst een expliciet hysteresis/cooldown- en active-lease-drain-contract plus een echte geïsoleerde staging-stresstest nodig.

## Eindverdict

**FAIL voor volledige holdout-acceptatie (7/8 PASS, 1/8 FAIL).** De unit suite is groen, maar dat overschrijft het ontbrekende autoscaling-thrashing/active-lease bewijs niet.
