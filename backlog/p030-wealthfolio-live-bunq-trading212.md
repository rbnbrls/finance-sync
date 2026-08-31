---
title: "Maak de Wealthfolio-export live voor bunq en Trading212"
status: todo
priority: 30
---

## Actuele status

De exporter, CLI, scheduler-job en contracttests bestaan. De live-acceptatie is
niet gehaald: `docs/wealthfolio-deployment-inventory.md` meldt dat er geen
workercontainer draait, de Wealthfolio push-instellingen ontbreken en er geen
bunq- of Trading212-credentials in de deployment staan. De eerdere `done`-
status was daarom onjuist en is teruggezet naar `todo`.

## Doel

Laat een geautoriseerde operator een veilige, herhaalbare sync uitvoeren die
bunq-activiteiten en Trading212-activiteiten/holdings naar de self-hosted
Wealthfolio-instance projecteert, zonder duplicaten of secrets in artifacts.

## Scope en randvoorwaarden

- Gebruik de bestaande connector- en Wealthfolio-exporterflows; bouw geen
  tweede integratiepad.
- Gebruik uitsluitend Coolify-managed secrets en bestaande connectorconfiguratie.
- Voer de eerste validatie uit tegen een gecontroleerde test-/stagingdoelgroep
  of live-installatie met expliciete toestemming; leg geen financiële waarden
  of credentials vast in git.
- De scheduler is optioneel zolang een gedocumenteerde handmatige trigger
  aantoonbaar herhaalbaar is, maar productieautomatisering vereist een aparte
  workerdeploy.

## User stories en acceptatiecriteria

### US1 — Deployment readiness

Als operator wil ik zien welke runtimecomponenten en instellingen ontbreken,
zodat ik de export veilig kan activeren.

- [ ] Inventariseer API-, worker- en Wealthfolio-doelconfiguratie; redacteer
  alle secrets en leg commit, datum en omgeving vast.
- [ ] Deploy een worker die `export_wealthfolio` kan registreren, of documenteer
  een herhaalbaar handmatig commando met de benodigde feature flags.
- [ ] Configureer `WEALTHFOLIO_SERVER_URL`, `WEALTHFOLIO_PASSWORD` en vereiste
  `WORKER_JOB_EXPORT_ENABLED` uitsluitend via de secret/configuratiestore.
- [ ] Voeg bunq- en Trading212-credentials/configs toe via de bestaande
  connection-API; controleer dat logs en API-responses geen secrets bevatten.

### US2 — End-to-end export

Als eigenaar wil ik actuele bunq- en Trading212-data in Wealthfolio zien.

- [ ] Test authenticatie tegen `/api/v1/auth/status` en de bestaande loginflow.
- [ ] Voer een beperkte bunq-sync en Trading212-sync uit; verifieer accounts,
  transacties en holdings in de Wealthfolio-UI/API.
- [ ] Controleer source identity, valuta/FX, holdings en foutmeldingen met de
  bestaande reconciliatie-output.
- [ ] Bewaar alleen redacted response-/UI-evidence of fixtures; geen bedragen,
  accountnummers, tokens of wachtwoorden.

### US3 — Idempotentie en operatie

Als operator wil ik de export veilig kunnen herhalen en terugvinden.

- [ ] Voer twee opeenvolgende runs uit en bewijs nul dubbele activiteiten,
  holdings of cashsnapshots; controleer delivery cursors en idempotency keys.
- [ ] Bewijs dat een fout/timeout geen gedeeltelijke corrupte projectie achterlaat
  en dat retry dezelfde connector-owned records veilig bijwerkt.
- [ ] Verifieer schedulerregistratie én een gedocumenteerde handmatige fallback;
  vermeld run-ID, status en relevante veilige metrics.
- [ ] Werk README/API/deploymentdocumentatie bij met configuratie, trigger,
  rollback en verificatiestappen.

## Verificatie vóór `done`

Voer `uv run pytest tests/test_wealthfolio_exporter.py tests/exporter/test_wealthfolio_client.py tests/exporter/test_wealthfolio_contract.py -q` uit, plus de beperkte live-/fixturetest en de twee-run-idempotentiecheck. Zet pas daarna `status: done` en voeg redacted evidence toe aan de PR.
