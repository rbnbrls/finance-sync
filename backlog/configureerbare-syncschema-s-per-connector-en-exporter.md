---
title: "Maak syncschema's per connector en exporter instelbaar via Sync Runs"
status: done
priority: 26
---

## Context

De pagina `Sync Runs` toont nu alleen de historie van uitgevoerde syncs. De
worker gebruikt globale, omgevingsafhankelijke intervallen voor bunq,
Trading212 en Wealthfolio-export. Daardoor kan een gebruiker niet per eigen
ingestion-verbinding of exporttarget bepalen wanneer data wordt opgehaald of
afgeleverd. Dit is vooral onpraktisch wanneer een bron maar eenmaal per dag
nodig is, of een export juist na iedere werkdag vóór openingstijd actueel moet
zijn.

Maak `Sync Runs` daarom ook de beheerpagina voor planning. Iedere actieve
connectorverbinding (ingestion) en ieder geconfigureerd exporttarget (export)
krijgt een eigen, tenant-scoped schema. Het schema is standaard: eenmaal per
werkdag, maandag t/m vrijdag, om 07:00 in de ingestelde tenant-tijdzone. De
globale workerinstellingen blijven uitsluitend veilige operationele grenzen en
zijn niet langer de gebruikersinstelling voor een individuele verbinding of
target.

## UX ontwerp

- Bovenaan `Sync Runs` staat naast de bestaande runhistorie een sectie
  **Planning**, met twee tabs of duidelijk gescheiden lijsten:
  **Ingestion** en **Export**.
- Elke rij toont de herkenbare connector-/exporternaam, de naam van de
  verbinding of het target, een ingeschakeld-schakelaar, een leesbare
  samenvatting (bijvoorbeeld `Elke werkdag om 07:00`), de tijdzone, de volgende
  geplande uitvoering en de uitkomst/tijd van de laatste run.
- Met **Schema wijzigen** opent een toegankelijke inline-editor of dialoog met
  frequentie, tijd, werkdagen/dagen en tijdzone. Een live, leesbare preview
  toont altijd de eerstvolgende drie uitvoermomenten voordat de gebruiker
  opslaat. Opslaan bevestigt de wijziging en ververst direct de volgende run;
  annuleren verandert niets.
- Ondersteun minstens `elke werkdag`, `elke dag`, `wekelijks` (dag of dagen van
  de week) en `elke N uur`; toon alleen velden die bij de gekozen frequentie
  horen. De UI valideert onmogelijke waarden en legt in gewone taal uit dat
  uitschakelen geplande runs stopt maar handmatig uitvoeren beschikbaar laat.
- De bestaande filters, statusoverzichten en paginering van de runhistorie
  blijven beschikbaar. De pagina blijft bruikbaar op mobiel, volledig via
  toetsenbord te bedienen en communiceert laden, opslaan en fouten met zichtbare
  tekst in plaats van alleen kleur of een toast.

## Acceptance criteria

- [ ] Er is een gemigreerd, tenant-scoped `sync_schedule`-model voor één
  uitvoerbare bron (`ingestion` + connection-ID) of bestemming (`export` +
  exporter/target-ID), met uniekheid per scope, `enabled`, een versieerbaar
  schema, IANA-tijdzone, `next_run_at`, `last_scheduled_at`, auditmetadata en
  zonder credentials, providerpayloads of financiële gegevens.
- [ ] Nieuwe actieve connectorverbindingen en exporttargets ontvangen atomair
  een eigen ingeschakeld standaardschema: maandag t/m vrijdag om 07:00 in de
  tenant-tijdzone (met een gedocumenteerde fallback wanneer die ontbreekt).
  Bestaande actieve configuraties worden via de migratie met exact dezelfde
  default voorzien; bestaande globale jobs blijven geen onverwachte extra run
  veroorzaken.
- [ ] Een geautoriseerde tenantbeheerder kan per connectorverbinding en per
  exporttarget het schema inzien, wijzigen, in- of uitschakelen en herstellen
  naar de standaard. Schemawijzigingen gelden alleen binnen de eigen tenant en
  worden met actor, oud/nieuw schema en tijdstip geaudit zonder secrets.
- [ ] Het API-contract biedt tenant-scoped endpoints om planning te lijstten,
  te lezen en te wijzigen, plus een serverberekende preview met de volgende
  drie momenten. Het contract valideert frequentie, minimaal één weekdag,
  IANA-tijdzone, `N` voor uurlijkse frequenties en een veilige minimale
  frequentie; het gebruikt consistente 4xx-fouten en OpenAPI-documentatie.
- [ ] Alleen gebruikers met de bestaande configuratie-/syncbeheerrechten mogen
  schema's wijzigen; read-only gebruikers kunnen hoogstens toegestane planning
  inzien. Object-ID's uit een andere tenant leveren geen bestaan- of
  planningsinformatie op.
- [ ] De worker haalt actieve schema's op en plant uitsluitend verschuldigde
  ingestion- of exportruns. Hij gebruikt de bestaande connector- en
  exporterflows, respecteert provider-rate limits en operationele feature
  flags, en voorkomt met database-lock/idempotentiesleutel dat meerdere worker
  replicas, scheduler-restarts of misfires dezelfde geplande uitvoering dubbel
  starten.
- [ ] Een uitgeschakeld schema start geen nieuwe geplande runs; een handmatige
  sync/export blijft expliciet mogelijk en verandert het schema niet. Een
  wijziging, inschakeling of uitschakeling berekent `next_run_at` meteen opnieuw
  en stopt veilig alleen nog niet gestarte geplande uitvoeringen.
- [ ] Tijdzone- en kalendergedrag is bepaald en getest: werkdag betekent
  maandag–vrijdag in de gekozen IANA-tijdzone (geen nationale feestdagen),
  zomer-/wintertijd levert geen dubbele lokale run op, een niet-bestaand lokaal
  tijdstip schuift naar het eerstvolgende geldige tijdstip en achterstallige
  runs worden gecoalesced tot maximaal één veilige catch-up per schema.
- [ ] De `Sync Runs`-pagina implementeert het hierboven beschreven ontwerp voor
  afzonderlijke Ingestion- en Export-planning, toont per item naam, status,
  menselijke schemaweergave, tijdzone, volgende run en laatste resultaat, en
  bevat een toegankelijke editor met live-preview, validatie, opslaan,
  annuleren, reset en duidelijke fout-/successtatussen.
- [ ] De bestaande sync-runhistorie, filters, autorisatie en read-endpoints
  blijven backwards compatible; runrecords vermelden voldoende provenance om
  een geplande run van een handmatige run te onderscheiden zonder private
  configuratie-inhoud te exposen.
- [ ] Unit-, API-, migratie-, scheduler- en end-to-end/UI-tests bewijzen de
  standaard voor nieuwe en bestaande configuraties, tenant-isolatie, RBAC,
  wijzigen/resetten/uitschakelen, preview, weekdagen, tijdzones en DST,
  misfires, restart/concurrentie-idempotentie, rate-limitgedrag, handmatige
  runs en behoud van de bestaande runhistorie.
- [ ] Beheer-, API- en deploymentdocumentatie beschrijven de schermbediening,
  standaard en fallbacktijdzone, ondersteunde frequenties, werkdagdefinitie,
  DST/misfiregedrag, vereiste rechten, operationele minimumfrequenties en hoe
  operators globale workergrenzen onderscheiden van tenantinstellingen.
