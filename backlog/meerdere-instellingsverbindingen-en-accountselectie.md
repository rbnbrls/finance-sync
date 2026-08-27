---
title: "Beheer meerdere bunq- en Trading212-verbindingen met accountselectie"
status: done
priority: 25
---

## Context

Wealthfolio Connect ondersteunt meerdere institutionele verbindingen en laat de
gebruiker beheren welke rekeningen worden gesynchroniseerd. finance-sync heeft
al CRUD- en test-endpoints voor connectorconfiguraties en een bijbehorende UI,
maar staat door de unieke index op `(tenant_id, provider_key)` slechts één
credentialset per provider toe. De sync kan bovendien niet per verbinding of
per providerrekening worden in- en uitgeschakeld.

De gratis self-hosted variant moet zonder commerciële planlimieten meerdere
bunq- of Trading212-logins kunnen beheren. Iedere verbinding moet afzonderlijk
testbaar, synchroniseerbaar, pauzeerbaar en controleerbaar zijn. Dit verhaal
bouwt voort op de bestaande connectors; er wordt geen externe aggregator
toegevoegd en alle providerverbindingen blijven read-only.

## Acceptance criteria

- [ ] Het datamodel ondersteunt meerdere credential/configuratierecords met
  dezelfde `provider_key` binnen één tenant; iedere record heeft een stabiele
  `connection_id`, een gebruikerslabel en een enabled/paused-status.
- [ ] Een migratie verwijdert de unieke beperking op
  `(tenant_id, provider_key)` op een achterwaarts compatibele manier en bewaart
  alle bestaande connectorconfiguraties.
- [ ] Accounts, sync-cursors en sync-runs zijn herleidbaar tot de specifieke
  verbinding waarmee ze zijn opgehaald. Gelijke externe account- of
  transactie-ID's uit twee verbindingen veroorzaken geen collisions of
  dataverlies.
- [ ] De connector-API en control-panel-UI kunnen meerdere bunq- en
  Trading212-verbindingen tonen, toevoegen, hernoemen, wijzigen, testen,
  pauzeren, hervatten en verwijderen zonder credentials terug te sturen naar
  de browser.
- [ ] Na een geslaagde verbindingstest kan de gebruiker de door de provider
  aangeboden accounts selecteren. Alleen geselecteerde accounts worden
  gesynchroniseerd en naar Wealthfolio geëxporteerd; een later gewijzigde
  selectie verwijdert geen reeds geïmporteerde historie zonder expliciete
  bevestiging.
- [ ] De handmatige sync-API kan één `connection_id` synchroniseren en de
  scheduler verwerkt alle actieve verbindingen onafhankelijk. Een fout in één
  verbinding blokkeert andere verbindingen niet.
- [ ] Per verbinding toont de API/UI minimaal: provider, label, status,
  geselecteerde accounts, laatste poging, laatste succesvolle sync en de
  laatste fout in geschoonde vorm.
- [ ] Toevoegen, wijzigen, testen, pauzeren, hervatten en verwijderen wordt in
  een tenant-scoped security-auditlog vastgelegd zonder secrets of financiële
  payloads. De auditlog is via een admin-endpoint opvraagbaar.
- [ ] Providercredentials blijven AES-256-GCM-versleuteld opgeslagen en komen
  niet voor in logs, API-responses, metrics, testfixtures of foutmeldingen.
- [ ] Unit-, migratie- en integratietests bewijzen twee gelijktijdige
  verbindingen voor dezelfde provider, accountselectie, isolatie bij fouten en
  correcte tenant-isolatie.
- [ ] OpenAPI- en gebruikersdocumentatie beschrijven het nieuwe
  verbindingsmodel, accountselectie, pauzeren/hervatten en herstel na fouten.

