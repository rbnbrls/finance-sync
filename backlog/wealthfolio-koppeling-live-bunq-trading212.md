---
title: "Wealthfolio-koppeling live maken: bunq + Trading212 data beschikbaar in Wealthfolio"
status: todo
priority: 30
---

## Context

De gebruiker wil alle benodigde data uit **bunq** en **Trading212** beschikbaar
hebben in **Wealthfolio** (zijn personal-finance app). Wealthfolio draait
self-hosted: Proxmox LXC container **104** (hostname `wealthfolio`), IP
**192.168.3.50**, web-UI op **http://192.168.3.50:8080** (REST API van de
instance is bereikbaar op die basis-URL).

finance-sync heeft de koppeling grotendeels **al gebouwd** — dit verhaal gaat
over het **live laten werken** tegen de echte instance en het **end-to-end
testen** (de roadmap-status "DONE" slaat op de code, niet op bewezen live
werking met echte data):

- Connectors: `src/finance_sync/connectors/bunq.py`, `src/finance_sync/connectors/trading212.py` (+ worker-jobs `worker_job_bunq_sync_enabled`, `worker_job_trading212_sync_enabled`)
- Exporter: `src/finance_sync/exporter/wealthfolio/` — `client.py` (HTTP-client, `authenticate()` + `import_activities()`, basis-URL default `http://192.168.3.50:8080`), `config.py`, `exporter.py` (ExportRun + per-account `wealthfolio_deliveries` cursor voor idempotente hervatting), `transaction_mapper.py` (canonical → Wealthfolio-format), `models.py`
- Wiring: `api/v1/exporters.py`, CLI-subcommand `finance-sync wealthfolio` (export CSV + push), scheduler-job `export_wealthfolio` (gated op `worker_job_export_enabled`, default alleen aan als de Wealthfolio push-target envvars gezet zijn)
- Settings/flags: `EXPORTER_WEALTHFOLIO_ENABLED`, `WEALTHFOLIO_OUTPUT_DIR`, `WEALTHFOLIO_DEFAULT_CURRENCY` (+ push-target vars zoals base-url/password)
- Tests: `tests/test_wealthfolio_exporter.py`, `tests/exporter/test_wealthfolio_client.py`, `tests/exporter/test_wealthfolio_contract.py`
- Docs: `docs/ROADMAP.md` (ms.4.f.4), `docs/roadmap-coverage.md` (G-13 PR #202, G-14 PR #214), `docs/ARCHITECTURE.md` §5

## Acceptatiecriteria

- [ ] **Inventarisatie**: huidige live-staat bepaald — wat draait al (env vars in de Coolify-deployment, scheduler-job actief?), wat ontbreekt er nog. Leg dit vast in de PR-beschrijving.
- [ ] **Live verbinding**: de exporter draait end-to-end tegen de echte Wealthfolio instance op `http://192.168.3.50:8080` (authenticatie werkt, accounts worden correct gemapt, data wordt geïmporteerd).
- [ ] **Bunq-data zichtbaar in Wealthfolio**: transacties (en waar van toepassing saldi/accounts) van de bunq-koppeling staan in Wealthfolio en zijn via de UI te zien.
- [ ] **Trading212-data zichtbaar in Wealthfolio**: posities/holdings en/of transacties van de Trading212-koppeling staan in Wealthfolio en zijn via de UI te zien.
- [ ] **Geen duplicaten/dataverlies** bij herhaalde runs: delivery-cursor/dedup (comment-veld met extern transactie-ID) werkt bewezen (twee opeenvolgende runs → geen dubbele regels).
- [ ] **Geautomatiseerd**: de export draait periodiek via de worker-scheduler (feature flag aan, `worker_job_export_enabled` + Wealthfolio push-target env vars correct gezet in de productieomgeving) óf via een gedocumenteerde, herhaalbare trigger. Indien de live-deployment (Coolify) de benodigde `WEALTHFOLIO_*`-instellingen mist, configureer deze via de Coolify API en documenteer elke wijziging.
- [ ] **Tests**: bestaande contracttests groen + nieuwe end-to-end/integratietest die de echte API-response van de live instance afdekt (of een opgenomen fixture van de echte respons als de live instance niet in CI bereikbaar is).
- [ ] **Geen geheimen in code**: credentials (bv. Wealthfolio-password, API-tokens) alleen via env vars, nooit in commits/PR's.
- [ ] **Documentatie** bijgewerkt (README/API/docs) over hoe de koppeling werkt, welke data erdoorheen stroomt en hoe te verifiëren.
- [ ] Wijzigingen via PR gemerged naar main.

## Verificatie

- Na merge: in de Wealthfolio-UI op http://192.168.3.50:8080 staan accounts/transacties/posities van bunq en Trading212.
- `finance-sync wealthfolio` (CLI) en de scheduler-job draaien zonder fouten en zijn idempotent.
