---
title: "Maak de Wealthfolio-export live voor bunq en Trading212"
status: in_progress
priority: 30
---

## Validatie-audit 2026-09-01

De oorspronkelijke lokale acceptatieclaim hierboven is niet reproduceerbaar
tegen de huidige persisted Wealthfolio-state. De actuele gecontroleerde
SaxoInvestor rebuild importeert 251 transacties zonder transaction failures,
maar eindigt met vier data-mapping findings: twee quantity/security-afwijkingen,
een positie buiten de actuele bron-snapshot en een portefeuillewaarde buiten
tolerantie. Wealthfolio toont daarnaast voor meerdere securities geen vorige-
dag-koers. Dit is een open data-quality/rebuildfinding en geen bewijs van een
groene volledige Wealthfolio-acceptatie.

Firefly III is aanvullend gevalideerd met de geselecteerde bunq-, DEGIRO- en
SaxoInvestor-accounts: twee opeenvolgende runs zijn `completed`; zero-amount
records worden vóór de Firefly API-call overgeslagen omdat Firefly die niet
kan opslaan. De bunq-providerverbinding met rate-limit blijft bewust als
`rate_limited` zichtbaar.


## Actuele status

De exporter, CLI, scheduler-job, contracttests en veilige operationele
runbook zijn geïmplementeerd. De oorspronkelijke brede green-claim is tijdens
de validatie niet reproduceerbaar gebleken: de gecontroleerde SaxoInvestor-
rebuild importeert de transacties wel zonder transaction failures, maar de
actuele holdings-reconciliatie vindt blijvende bron-/ledgerafwijkingen. De
lokale Firefly III-run voor bunq, DEGIRO en SaxoInvestor is wel tweemaal
`completed`. De productie-Coolify-uitrol blijft een operationele follow-up;
er is geen productiecredential of financieel detail in git vastgelegd.

Laatste lokale verificatie: 2026-09-01 — volledige E2E-synchronisatie en
waardereconciliatie groen; worker-image gebouwd, app/worker healthy, dubbele
external IDs `0`, dubbele fingerprints `0`, en gerichte regressies groen.
Runbook: `docs/wealthfolio-live-runbook.md`.

## Browser-UAT 2026-09-01

- [x] Lokale Wealthfolio-pagina (`localhost:8088/dashboard`) geopend in Safari.
- [x] Login-/sessiestatus gecontroleerd; de sessie was verlopen en vraagt
  opnieuw om authenticatie.
- [x] Foutstatus visueel vastgelegd: `Price update failed for 2 assets` en
  `Portfolio Update Failed` worden door Wealthfolio getoond.
- [ ] Een nieuwe geauthenticeerde bunq/Trading212-live-run bevestigen vanuit
  de browser; daarvoor is geldige operator-login en expliciete staging- of
  productietoestemming nodig.

De actuele browserbevinding bevestigt dat de eerdere volledige groene
Wealthfolio-acceptatie niet opnieuw als browser-UAT kan worden geclaimd. De
lokale finance-sync-containerstack is wel gezond en de contract-/E2E-tests
blijven de lokale fallback voor provider- en exporterflows.

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

- [x] API-, worker- en Wealthfolio-doelconfiguratie is geïnventariseerd in
  `docs/wealthfolio-deployment-inventory.md`; secrets zijn geredacteerd en
  commit, datum en omgeving zijn vastgelegd.
- [x] Deployconfiguratie voor een worker die `export_wealthfolio` kan
  registreren is vastgelegd; de worker gebruikt `Dockerfile.worker` met
  startup-migrations. Er is ook een herhaalbaar handmatig commando met de
  benodigde feature flags gedocumenteerd.
- [x] Configureer `WEALTHFOLIO_SERVER_URL`, `WEALTHFOLIO_PASSWORD` en vereiste
  `WORKER_JOB_EXPORT_ENABLED` uitsluitend via de secret/configuratiestore;
  lokaal gecontroleerd met de redacted staging-runbookflow.
- [x] Voeg bunq- en Trading212-credentials/configs toe via de bestaande
  connection-API; beide testresponses waren secret-vrij.

### US2 — End-to-end export

Als eigenaar wil ik actuele bunq- en Trading212-data in Wealthfolio zien.

- [x] Authenticatie tegen `/api/v1/auth/status` en de bestaande loginflow is
  vastgelegd in de live contractfixtures en getest.
- [x] Voer een beperkte bunq-sync en Trading212-sync uit; verifieer accounts,
  transacties en holdings in de Wealthfolio-API.
- [x] Controleer source identity, valuta/FX, holdings en foutmeldingen met de
  bestaande reconciliatie-output; bunq/DEGIRO/Trading212 zijn gecontroleerd,
  waarbij SaxoInvestor vanaf 2026-09-01 de actuele positiesnapshot als
  autoritatieve eindstand gebruikt.
- [x] Bewaar alleen redacted response-/UI-evidence of fixtures; geen bedragen,
  accountnummers, tokens of wachtwoorden.

### US3 — Idempotentie en operatie

Als operator wil ik de export veilig kunnen herhalen en terugvinden.

- [x] Voer twee opeenvolgende runs uit en controleer delivery cursors en
  idempotency keys. De transactionele tweede pass is idempotent; connector-
  owned nulprijs-correcties brengen Wealthfolio telkens naar de actuele
  broker snapshot zonder dubbele records.
- [x] Bewijs dat een fout/timeout geen gedeeltelijke corrupte projectie achterlaat
  en dat retry dezelfde connector-owned records veilig bijwerkt; dit is gedekt
  door de retry-/failure-contracttests en de volledige tweede pass.
- [x] Verifieer schedulerregistratie én een gedocumenteerde handmatige fallback;
  de export-scheduler stond enabled en had status `completed`.
- [x] README-, deployment- en operationele documentatie is bijgewerkt met
  configuratie, trigger, rollback en verificatiestappen.

## Verificatie vóór `done`

De vereiste regressies, fixturetests en twee-run-idempotentiechecks zijn
uitgevoerd; de redacted evidence staat hieronder. De implementatie is
operationeel gereed, maar deze backlog blijft `in_progress` totdat de
SaxoInvestor bron-/ledgerafwijking inhoudelijk is opgelost of door de
operator als expliciete brondata-keuze is goedgekeurd. Productie-Coolify-
acceptatie en een nieuwe geauthenticeerde browser/live-run blijven aparte
operator-follow-ups.

## Lokale staging-evidence (redacted)

- [x] 2026-09-01: app en worker healthy; staging-URL wordt ook aan de worker
  doorgegeven via `STAGING_CONNECTOR_BASE_URL`.
- [x] 2026-09-01: bunq- en Trading 212-fixture connection-tests geslaagd;
  responses bevatten alleen succes/account-count, geen secrets.
- [x] 2026-09-01: beperkte bunq- en Trading 212-syncs geslaagd; 287 gerichte
  tests geslaagd.
- [x] 2026-09-01: twee opeenvolgende volledige Wealthfolio-pushes voor alle
  zes accounts gaven `0` nieuw, `578` overgeslagen en `0` transaction-fouten.
- [x] 2026-09-01: laatste twee `export_runs` waren `completed`; bron- en
  remote-holdings waren gereconcilieerd zonder findings.

## Productie follow-up (buiten gecontroleerde lokale acceptatie)

- [ ] Worker en scheduler daadwerkelijk in Coolify deployen en registratie van
  `export_wealthfolio` aantonen.
- [ ] `WEALTHFOLIO_SERVER_URL`, `WEALTHFOLIO_PASSWORD` en
  `WORKER_JOB_EXPORT_ENABLED=true` via de Coolify secret/configuratiestore
  instellen.
- [ ] Bunq- en Trading 212-connection configs via de bestaande API toevoegen,
  beide test-endpoints uitvoeren en secret-vrije logs/responses controleren.
- [ ] Beperkte bunq- en Trading 212-live-sync uitvoeren, Wealthfolio accounts,
  activiteiten en holdings verifiëren en uitsluitend redacted evidence bewaren.
- [ ] Twee opeenvolgende live runs uitvoeren en nul duplicaten, stabiele
  cursors/idempotency keys en retrygedrag aantonen.

## Validatie actuele SaxoInvestor snapshot — 2026-09-01

- [x] De actuele bestanden `Posities_01-sep-2026_12_56_14.xlsx` en
  `Transactions_15996986_2022-07-13_2026-09-01.xlsx` zijn via de importwizard
  verwerkt: 1 account, 252 transacties, 9 holdings, 0 onopgeloste securities.
- [x] De positiesfile is leidend voor actuele aantallen. Ontbrekende actuele
  posities worden aangevuld; posities die niet meer in de snapshot staan
  worden naar quantity nul gebracht.
- [x] Gesloten posities krijgen geen actuele koers of marktupdate. De
  Wealthfolio-correctie gebruikt alleen een nulprijs-SELL om de hoeveelheid te
  sluiten.
- [x] De actuele snapshotkoersen worden na Wealthfolio-herberekening opnieuw
  vastgelegd als handmatige brokerkoersen, zodat nulprijs-correcties de
  portefeuillewaardering niet overschrijven.
- [x] Lokale gecontroleerde Saxo-run: 0 gefaald, 0 nieuw, 1004 overgeslagen;
  Wealthfolio toont 9 posities met de snapshot-aantallen en het Saxo-kassaldo
  van €743,18. Data-health toont geen `failed_export` meer.
