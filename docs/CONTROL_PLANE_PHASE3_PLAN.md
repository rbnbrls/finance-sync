# Implementatieplan fase 3 — GUI-control plane

## Doel

Maak het bestaande dashboard tot één operationele control plane voor de
volledige datastroom: bron, synchronisatie, canonieke dataset, datakwaliteit
en bestemming. De startpagina gebruikt één tenant-scoped overview-contract en
geeft bij ieder probleem een begrijpelijke vervolgstap.

## Scope

1. **Statusheader en dataketen**
   - algemene status (`healthy`, `attention_required`, `sync_failed`,
     `partial`) met Nederlandse labels;
   - laatste controle (`generated_at`) en bron/centrale database/bestemming;
   - aantallen verbindingen, mislukte syncs, open issues en falende
     bestemmingen.
2. **Actiecentrum**
   - severity-badge, oorzaak/impact, concrete actie en inline uitvoerstatus;
   - acties blijven API-gedreven en gebruiken geen onveilige serverwaarden in
     HTML; alle tekst wordt escaped;
   - sync- en export-retry sturen POST via de bestaande fase-2 endpoints;
     bekijken/configureren navigeert naar de bestaande schermen.
3. **Operationele secties op de overview**
   - verbindingen met status, laatste poging/succes/fout en volgende run;
   - recente syncs met leesbare status, duur/aantallen, foutcategorie en
     retry/details;
   - datakwaliteit met freshness, brondekking en unresolved securities;
   - bestemmingen met health/exportstatus, laatste fout, volgende run en
     bestaande beheeracties.
4. **Robuustheid en toegankelijkheid**
   - expliciete loading-, empty- en error-states met retry;
   - responsive grid, `aria-live`, knoppen met labels en keyboard-focus;
   - één overview-request; de bestaande detailpagina’s blijven lazy-loaded.

## Uitvoeringsvolgorde

1. Voeg fase-3 contract-/renderingtests toe voor de dashboard markup en de
   belangrijkste endpoint-/actiepaths.
2. Voeg styling en HTML-blokken toe voor status, issues, connections, syncs,
   quality en destinations.
3. Vervang de bestaande overview-loader door de control-plane overview-call
   en voeg veilige renderhelpers en actiehandlers toe.
4. Draai gericht de GUI/control-plane tests, daarna Ruff, Pyright en de
   volledige unit-testjob met coverage.

## Verificatie

- Dashboard bevat alle operationele secties en de overview-endpoint.
- Gezonde, lege, gedeeltelijke en mislukte responses tonen elk een leesbare
  toestand.
- Issue cards ontsluiten precies één concrete actie; retry geeft succes of
  een inline fout terug zonder secrets/stack traces te tonen.
- API-waarden worden escaped voordat ze in `innerHTML` komen.
- `uv run ruff check src tests` en `uv run ruff format --check src tests` zijn
  groen.
- `uv run pyright -p pyproject.toml src` en de testconfig zijn groen.
- Unit tests inclusief coverage-gate zijn groen.

## Buiten scope

Nieuwe backenddomeinmodellen, persistente issues, permissie-/concurrency-
wijzigingen en nieuwe connectors. Deze horen bij fase 4/5 of bestaan al in
de fase-1/2 backend.
