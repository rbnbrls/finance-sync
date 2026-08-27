---
title: "Breid connectors uit met holdings-, security- en FX-ingestie"
status: done
priority: 31
---

## Context

De generieke connectorflow verwerkt momenteel alleen accounts en transacties.
Er bestaat al een tijdgebonden `Holding`-model en transacties hebben in de
database velden voor een security, basisvaluta en wisselkoers, maar de
connector-DTO's en de sync-orchestrator vullen deze gegevens niet. De
Trading212-connector heeft daarom een losse `fetch_portfolio()`-methode die niet
door de generieke syncflow wordt verwerkt. Een DEGIRO-pensioenconnector heeft
dezelfde ontbrekende bouwstenen nodig om posities, ISIN's, marktwaarden en
valuta correct te importeren.

Deze story maakt portfolio-ingestie een provider-onafhankelijke capability en
houdt bestaande connectors achterwaarts compatibel.

## Dependencies

Geen. Dit is de technische basis voor de DEGIRO-pensioenstories en verbetert
ook de bestaande Trading212-ingestie.

## Acceptance criteria

- [x] De connector-SDK bevat provider-neutrale raw en canonical modellen voor
  een holding/positiesnapshot met minimaal externe account-ID,
  observatietijd, quantity, cost basis, marktwaarde, prijs en bijbehorende
  valuta's.
- [x] Transacties en holdings kunnen een provider-neutrale securityreferentie
  meegeven met minimaal ISIN en optioneel FIGI, ticker, naam, beurs/venue en
  noteringsvaluta; connectors kennen of bepalen geen interne database-ID's.
- [x] Canonical transacties kunnen `amount_in_base`, `base_currency_code` en
  `fx_rate` doorgeven en de orchestrator schrijft deze velden bij create én
  update naar het bestaande transactiemodel.
- [x] `tax` wordt als canoniek transactietype toegevoegd en door read-API's,
  filters, exporters en OpenAPI als additieve waarde ondersteund, zodat
  ingehouden dividendbelasting niet als generieke fee verloren gaat.
- [x] Een expliciete capability (`holdings`) bepaalt of de orchestrator
  holdings ophaalt. Connectors zonder die capability blijven zonder
  gedragswijziging door alle bestaande contracttests gaan.
- [x] De orchestrator resolveert securityreferenties bij voorkeur op ISIN,
  koppelt zowel transacties als holdings aan dezelfde canonieke security en
  stuurt niet-oplosbare of ambigue instrumenten naar de bestaande unresolved
  security-flow zonder ze stil aan een verkeerd instrument te koppelen.
- [x] Holdings worden idempotent opgeslagen per tenant, verbinding, account,
  security, observatietijd en bron. Het opnieuw aanbieden van dezelfde snapshot
  maakt geen dubbele rows; een gewijzigde snapshot is aantoonbaar
  tijdsversieerbaar en overschrijft geen historie.
- [x] Account-, transactie-, holding- en securitywrites voor één sync zijn
  atomair of hebben een gedocumenteerde herstelstrategie die voorkomt dat een
  halve portfolio als succesvol wordt gemarkeerd.
- [x] De bestaande Trading212 `fetch_portfolio()`-data gebruikt de nieuwe
  holdings-capability en produceert via de normale orchestrator actuele
  holdings met gekoppelde securities.
- [x] Syncresultaten, outbox-events en metrics rapporteren aantallen holdings en
  unresolved securities zonder financiële waarden, identifiers of volledige
  providerpayloads als metriclabels/logvelden op te nemen.
- [x] Unit-, contract-, migratie- en integratietests dekken backwards
  compatibility, holding-upserts, historische snapshots, ISIN-resolutie,
  unresolved instruments, FX/basebedragen, `tax` en rollback bij fouten.
- [x] Connector- en architectuurdocumentatie beschrijven de capability,
  modellen, idempotentiesleutel, securityresolutie en een voorbeeldconnector.
