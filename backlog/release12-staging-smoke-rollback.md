---
title: "Voer staging smoke en rollback-evidence uit"
status: done
priority: 20
---

## Context

De release moet aantonen dat de gemodulariseerde applicatie operationeel
start, synchroniseert en veilig kan worden teruggedraaid zonder een
productiedowngrade.

## Acceptance criteria

- [x] Draai staging smoke met uitsluitend synthetische financiële data.
- [x] Controleer readiness, health, sync, outbox en exporter smoke flows.
- [x] Controleer image rollback met backward-compatible database migrations.
- [x] Documenteer dat rollback via application-image rollback verloopt en
  niet via blind schema-downgrade.
- [x] Leg commit, image-tag, omgeving, datum en artifact-links vast.
- [x] Werk README, ARCHITECTURE, DATABASE, UPGRADE en rollbackrunbook bij.

## Implementatie en verificatie

- `scripts/release_smoke.py` controleert liveness, readiness, login, de
  DB-backed read, synthetische bunq-sync, sync-run/outbox-evidence en de
  Wealthfolio exporter-run/readback.
- De smoke schrijft uitsluitend redacted evidence naar
  `staging-smoke-evidence.json`: commit, immutable image-tag, omgeving,
  timestamp, checkstatussen en artifact-link. Tokens, wachtwoorden en
  financiële payloads worden niet opgeslagen.
- De releaseworkflow uploadt log en evidence als
  `release-staging-smoke-${{ github.sha }}` en blokkeert promotion bij falen.
- README, ARCHITECTURE, DATABASE, UPGRADE en RELEASING beschrijven de
  synthetische stagingflow en image rollback met backward-compatible
  migrations. Een blind production schema-downgrade is expliciet verboden.

Verificatie:

```text
uv run pytest tests/test_release12_staging_smoke.py -q
3 passed

uv run ruff check tests/test_release12_staging_smoke.py
All checks passed

uv run python -m py_compile scripts/release_smoke.py
Geslaagd

git diff --check
Geslaagd
```
