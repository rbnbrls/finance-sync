---
title: "Maak release smoke-evidence herleidbaar"
status: todo
priority: 20
---

## Context

Staging smoke, image build en service-gates moeten samen één herleidbaar
releasebewijs opleveren.

## Dependencies

Release 13 service-gates en security-evidence.

## Acceptance criteria

- [ ] Definieer één smoke-run met image-tag, commit, schema-versie en
  synthetische dataset.
- [ ] Controleer readiness, authentication, read API, sync, outbox en exporter.
- [ ] Sla logs, JUnit-output en smoke-summary op als artifacts.
- [ ] Laat ontbrekende artifacts of een verkeerde image-tag de job falen.
- [ ] Documenteer reproduceerbare lokale/CI-aanroep.
