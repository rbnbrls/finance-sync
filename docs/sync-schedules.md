# Sync-schema's per connector en exporter (Planning)

De pagina **Sync Runs** is naast de runhistorie ook de beheerpagina voor
planning. Iedere actieve connectorverbinding (ingestion) en ieder
geconfigureerd exporttarget (export) heeft een eigen, tenant-scoped
`sync_schedules`-rij die bepaalt **wanneer** een geplande run draait.

## Model

Eén rij = één uitvoerbare bron of bestemming:

| scope       | target_id                                   |
|-------------|---------------------------------------------|
| `ingestion` | connection-id (Credential-rij)              |
| `export`    | exporter-key (`wealthfolio`, `actual-budget`) |

De rij bevat `enabled`, een versieerbaar `schedule`-JSONB
(`schema_version`), IANA-`timezone`, `next_run_at` (UTC), `last_scheduled_at`
(UTC), `last_run_at` / `last_run_status` / `last_run_error` (gesaneerd),
optimistische-lock `version` en auditmetadata (`created_by`/`updated_by`).
De rij bevat **nooit** credentials, providerpayloads of financiële
gegevens.

Uniekheid: `(tenant_id, scope, target_id)` — maximaal één schema per
scope per tenant.

## Standaard en fallbacktijdzone

Elke nieuwe actieve verbinding / elk exporttarget ontvangt atomair een
ingeschakeld standaardschema:

- frequentie `weekdays`, tijd `07:00`, maandag t/m vrijdag
- tijdzone `Europe/Amsterdam` (de gedocumenteerde tenant-default)
- fallback: `UTC` wanneer de tijdzone niet resolveert

Bestaande actieve configuraties krijgen via migratie `0020` exact
dezelfde default (idempotent, `INSERT … ON CONFLICT DO NOTHING`). De
migratie start geen runs: `next_run_at` wordt direct naar het eerste
toekomstige moment (volgende werkdag 07:00 in de tenant-tijdzone)
berekend — strikt in de toekomst, dus er kan niets op migratiedag
afgaan; de globale jobs blijven hun normale vangnet.

## Ondersteunde frequenties

| frequentie | velden                                   |
|------------|------------------------------------------|
| `weekdays` | `time` (ma–vr)                           |
| `daily`    | `time`                                    |
| `weekly`   | `time`, `weekdays` (minimaal één dag 0–6)|
| `hourly`   | `interval_hours` (1–168)                 |

Validatie (server-side, consistente 422): onbekende frequentie, ongeldige
`HH:MM`-tijd, lege `weekdays`, niet-gehele / buiten-bereik
`interval_hours` en onbekende IANA-tijdzone. De UI toont alleen velden
die bij de gekozen frequentie horen.

## Werkdagdefinitie, DST en misfires

- Werkdag = maandag t/m vrijdag in de IANA-tijdzone van het schema.
  Nationale feestdagen tellen niet mee.
- DST: `next_run_at` wordt in de lokale zone berekend. Een niet-bestaand
  lokaal tijdstip (voorjaar) schuift naar het eerstvolgende geldige
  moment; een terugdraaiing produceert nooit twee runs voor één
  wandkloktijd. "Elke N uur" blijft verankerd (geen drift).
- Misfires: de worker coalesceert achterstallige schema's tot maximaal
  één veilige catch-up per schema per tick. Een schema dat meer dan 7
  dagen achterloopt wordt gereset naar het volgende toekomstige moment
  in plaats van een catch-up te draaien.

## API

| endpoint | methode | rechten | beschrijving |
|----------|---------|---------|--------------|
| `/api/v1/sync-schedules` | GET | `sync:read` | lijst (filter `?scope=`) |
| `/api/v1/sync-schedules/{id}` | GET | `sync:read` | detail |
| `/api/v1/sync-schedules/{id}/preview` | GET | `sync:read` | volgende 3 momenten (serverberekend) |
| `/api/v1/sync-schedules/{id}` | PATCH | `sync:write` | wijzig schema/tijdzone/enabled; `version` → 409 bij conflict |
| `/api/v1/sync-schedules/{id}/reset` | POST | `sync:write` | standaard herstellen |
| `/api/v1/sync-schedules/{id}/disable` | POST | `sync:write` | uitschakelen |
| `/api/v1/sync-schedules/{id}/enable` | POST | `sync:write` | inschakelen (+ `next_run_at` herberekend) |

- Read-only gebruikers kunnen planning inzien (`sync:read`); alleen
  gebruikers met `sync:write` (de bestaande configuratie-/syncbeheer-
  rechten) mogen wijzigen.
- Object-ID's uit een andere tenant gedragen zich exact als niet-
  bestaande ID's (uniforme 404; geen bestaan- of planningsinformatie).
- Elke wijziging wordt geaudit (actor, oud/nieuw schema, tijdstip) met
  secret-redactie; de preview gebruikt dezelfde pure berekening als de
  worker.

## Worker

- Een minuut-tick (`run_scheduled_syncs`, gated door
  `WORKER_JOB_SCHEDULES_ENABLED`) selecteert enabled schema's waarvan
  `next_run_at <= now`.
- Het claimen is een guarded `UPDATE ... WHERE last_scheduled_at <
  cutoff`: meerdere replicas, scheduler-restarts en misfires kunnen een
  geplande uitvoering nooit dubbel starten (de claim-update is de
  idempotentiesleutel). De claim wordt **gecommit vóór** de uitvoering
  begint — een rollback bij sessie-sluiting zou de guard anders
  stilletjes ongedaan maken en een tweede replica zou hetzelfde venster
  alsnog claimen en draaien.
- Uitvoering gebruikt de bestaande connector-/exporterflows
  (`SyncOrchestrator`, `WealthfolioExporter`), respecteert provider-rate
  limits (de connectors' `RateLimiter`) en de operationele feature flags.
- Een uitgeschakeld schema start geen nieuwe geplande runs; handmatige
  sync/export blijft expliciet mogelijk en verandert het schema niet.
- Een verweesd schema (verbinding verwijderd) wordt overgeslagen en
  gereset — geen ghost-runs, geen fout-rijen in de historie.

## Globale workergrenzen vs. tenantinstellingen

De `WORKER_JOB_*`-instellingen zijn **operationele grenzen**, geen
gebruikersinstelling:

| setting | rol |
|---------|-----|
| `WORKER_JOB_BUNQ_SYNC_ENABLED` | gate voor bunq-ingestion-schema's |
| `WORKER_JOB_TRADING212_SYNC_ENABLED` | gate voor Trading212-schema's |
| `WORKER_JOB_EXPORT_ENABLED` | gate voor export-schema's |
| `WORKER_JOB_SCHEDULES_ENABLED` | master-schakelaar voor de planningstik |

Wanneer een gate uit staat, worden de bijbehorende schema's niet
uitgevoerd (log + skip) maar blijven ze enabled; het weer inschakelen
hervat ze. Operators die de legacy-globale jobs willen behouden naast de
tenantplanning kunnen dat laten staan — de tenant-schema's zijn de
gebruikersinstelling, de globale jobs de vangnetten.

## UI

De Planning-sectie toont per rij: naam (herkenbaar uit de verbindings-
configuratie), status, menselijke schemaweergave (bijv. `Elke werkdag om
07:00`), tijdzone, volgende run en laatste resultaat. De editor biedt
frequentie, tijd, dagen, tijdzone, live preview (drie momenten), opslaan,
annuleren en standaard herstellen; laden/opslaan/fouten worden met
zichtbare tekst gecommuniceerd. De pagina blijft mobiel bruikbaar en
volledig via toetsenbord bedienbaar.
