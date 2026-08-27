---
title: "Monitor disaster-recovery RPO en RTO"
status: done
priority: 25
---

## Context

Release 18 automatiseert herstelstappen, maar er is nog geen periodieke
monitoring die aantoont dat de afgesproken RPO/RTO daadwerkelijk wordt gehaald.

## Dependencies

Release 18 automated DR runbook.

## Acceptance criteria

- [x] Voer periodiek een geïsoleerde restore-check uit met synthetische data.
- [x] Meet restoreduur, laatste bruikbare backup, replay-lag en herstelstatus.
- [x] Maak alerts bij overschrijding van RPO/RTO.
- [x] Publiceer status zonder tenantdata, credentials of financiële waarden.
- [x] Link iedere failure aan een runbook en eigenaar.

## Implementatie en verificatie

- `scripts/dr_sla_monitoring.py` voert de geïsoleerde restore-check uit met
  synthetische data (CI `dr-sla-monitoring` job: pg_dump → pg_restore op een
  tweede geïsoleerde database) en publiceert restoreduur, leeftijd laatste
  bruikbare backup, replay-lag en herstelstatus.
- `config/dr-sla-monitoring.json` bevat RPO 15 minuten, RTO 30 minuten,
  check-interval, alert-dedup, eigenaar en runbook-link.
- Alerts bij RPO/RTO-overschrijding: Grafana-regels
  `finance-sync-dr-rpo-breach` / `finance-sync-dr-rto-breach` (provisioned
  in `docker/grafana/provisioning/alerting/finance-sync.rules.yaml`),
  gededupliceerd per tenant per interval.
- Statuspublicatie bevat geen tenantdata, credentials of financiële waarden
  (credential-redactie ook in failure-payloads; tenant-id's worden
  gereduceerd tot operationele labels).
- Iedere failure linkt naar `docs/AUTOMATED_DR_RUNBOOK.md` en eigenaar
  `finance-platform-oncall`.
- Verificatie: 17 tests (incl. 8 holdout-scenario's), Ruff en Pyright
  geslaagd; CI job `dr-sla-monitoring` draait periodiek en archiveert
  evidence als artifact.
