# Control-plane fase 2 — implementatieplan

## Doel

Maak de bestaande control-plane-projectie bruikbaar voor herstelacties. Een
gebruiker moet een verbinding, sync-run en bestemming veilig kunnen inspecteren
en een mislukte sync of export opnieuw kunnen uitvoeren, zonder secrets of data
van een andere tenant te zien.

## Scope en volgorde

### Slice 1 — uniforme operationele status

- Voeg de vaste sync-foutcategorieën toe: `authentication`,
  `provider_unavailable`, `rate_limited`, `validation`, `data_mapping`,
  `database`, `unknown`.
- Classificeer connector-, HTTP- en infrastructuurfouten op één plek en sla
  alleen een begrensde, gesaneerde boodschap op.
- Breid de connection-projectie uit met foutcategorie en testresultaatvelden;
  behoud bestaande connection- en connector-endpoints als mutatiebron.
- Breid de control-plane-schema’s uit met expliciete sync-runstatus,
  destination/exportstatus en herstelacties.

### Slice 2 — sync-run detail en retry

- Voeg `GET /api/v1/sync-runs/{run_id}` toe.
- Laat het detail connector, connection, tijden, status, aantallen,
  unresolved securities, cursor/watermark, foutcategorie en gesaneerde fout
  teruggeven.
- Voeg `POST /api/v1/sync-runs/{run_id}/retry` toe. Alleen een mislukte,
  connection-scoped run mag worden herhaald; de credential en run worden altijd
  binnen de tenant van de ingelogde gebruiker opgezocht.
- Laat retry dezelfde `SyncOrchestrator` gebruiken als een handmatige sync en
  retourneer de nieuwe run-link.

### Slice 3 — herstelacties in overview

- Neem mislukte export-runs op in de centrale issue-feed, met een directe retry-
  actie.
- Zorg dat destination- en export-read/detail/retry-endpoints tenant-scoped
  zijn; behoud de bestaande destination wizard en exportimplementaties.
- Voeg tests toe voor tenant-isolatie, gesaneerde fouten, foutcategorieën,
  retry-voorwaarden en action paths.

## Verificatie

1. Ruff lint en format.
2. Pyright voor `src` en tests.
3. Unit tests voor classificatie, projections en retry guards.
4. Volledige unit-testset met coverage-gate.
5. Alembic upgrade/downgrade en integratietests als PostgreSQL/Redis lokaal
   beschikbaar zijn.

## Buiten scope

De GUI (fase 3), persistente control-plane-issues (fase 5), nieuwe connectors,
budgetfunctionaliteit en analytische uitbreidingen.
