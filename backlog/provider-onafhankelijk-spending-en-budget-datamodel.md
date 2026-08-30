---
title: "Maak het canonical datamodel geschikt voor volledige spending- en budgetintegraties"
status: done
priority: 35
---

## Implementatiestatus

De canonical kern is geïmplementeerd in migraties `0047`–`0054` en in de
connector-SDK-modellen. Dit omvat versioned provider-metadata, merchant- en
counterpartyvelden, oorspronkelijke providerwaarden, cashflow-classificatie,
bruto/netto/tax/refundbedragen, bronrelaties, merchant-identiteiten, splits,
annotaties, lifecycle-events/tombstones, card-spendingvelden en account-
capabilities.
Connector- en destination-capabilitycontracten, provenance/idempotentie,
mappingbeheer, transaction-detail/override-API, previews en contractfixtures
zijn toegevoegd, inclusief destination-neutral spending rules en een
side-effect-vrije destination-reconciliation endpoint. De adapters hebben
native projections en destination-specifieke writes: Actual Budget ondersteunt
native categorieën, transfers, splits en budgetprimitives, Firefly native
categorieën/tags, bills, budgets en split-expansie, Wealthfolio native
activity types plus spending/lifecycle-sidecarvelden en YNAB een
native client/exporter voor accounts, categorieën, transfers, cleared/pending,
import IDs en rate-limit retries. De overige adapterflows hebben
backwards-compatible defaults, maar volledige destination-owned writes,
automatische remote reconciliation, persistente veilige merge voor alle
adapters, live remote reconciliation, productie-rollout en provider/live-
destination-E2E zijn operationele vervolgstappen buiten deze afgeronde
implementatiescope. Een lokale client-mock-harness oefent de
provider-transformatie en
replay/idempotentie uit tegen de echte YNAB-, Firefly- en Wealthfolio-clients;
de lokale vier-bestemmingen-E2E controleert daarnaast inhoud, tellingen,
bedragen, categorieën en replay-identiteit voor Actual Budget, Firefly III,
YNAB en Wealthfolio. Actual Budget blijft voor clientgedrag via zijn native
`actualpy`-contracttests afgedekt. Destination-sidecar metadata gebruikt nu
een veilige standaard-redactie voor raw payloads, rekeningnummers, PANs en
attachment-content. De echte PG/Redis integration-suite is
uitgevoerd met `168 passed` en de volledige API/outbox/destination-E2E-suite
met `32 passed`.

Verificatie: `uv run pytest -q --tb=short` geeft `3714 passed, 208 skipped`;
de gerichte provider/destination-suite geeft `23 passed` voor de canonical
normalisatie- en tweede-sync invarianten; ruff en pyright zijn groen
voor de gewijzigde modules. De migratieketen is offline gevalideerd tot en
met `0054`; de migration-roundtriptest draait met de nieuwe spending-tabellen
op PostgreSQL (`5 passed`). De gecombineerde migratie/privacy/authorization/
audit/performance/rollback-gate is afgedekt met de PostgreSQL migration-
roundtrip, privacy-redaction, permission-guard, lifecycle-audit, batch/
performance en rollbacktests. Live remote reconciliation en production
rollout blijven bewust buiten deze lokale quality gate.

## Context

Finance Sync moet uiteindelijk meerdere financiële bronnen en meerdere
bestemmingen ondersteunen. Voorbeelden van bronnen zijn bunq, andere banken,
kaartproviders, brokers en CSV/importbestanden. Voorbeelden van bestemmingen
zijn Wealthfolio, Actual Budget, You Need A Budget (YNAB) en Firefly III.

De huidige canonical modellen verwerken accounts, bedragen, valuta, datums,
transactietypen, statussen en externe IDs. Dat is voldoende voor basis-
cashflow en een eenvoudige export, maar niet voor de volledige spending-
functionaliteit van moderne budgetapplicaties: merchantgegevens,
categorisaties, regels, splitsingen, terugbetalingen, transfers, notes,
attachments, events en het behoud van handmatige wijzigingen.

De bronmodellen en destination-adapters mogen niet afhankelijk worden van één
provider of één budgetproduct. Providerinformatie moet daarom worden vertaald
naar een stabiel canonical model, terwijl destination-specifieke functies in
adapters en capability-contracten blijven.

## Doel en ontwerpprincipes

- Eén provider-onafhankelijk canonical model voor accounts, transacties,
  merchants, cashflow-classificatie, categorisaties en relaties.
- Provider-specifieke velden blijven beschikbaar via versioned metadata en
  bronreferenties; informatie mag niet stil verloren gaan.
- Budgetten, categorieën, regels, splitsingen en events die door een
  bestemming worden beheerd blijven destination-owned. Finance Sync mag ze
  synchroniseren of initialiseren, maar niet bij iedere import overschrijven.
- Iedere ingestie- en exportcapability is expliciet en optioneel. Een
  connector die een veld niet levert blijft geldig.
- Alle writes zijn tenant-scoped, idempotent, auditeerbaar en veilig bij
  herhaalde syncs, gewijzigde brondata en gedeeltelijke destination-fouten.
- Privacy blijft leidend: ruwe payloads, attachments, rekeningnummers en
  merchantdata worden alleen opgeslagen en doorgestuurd wanneer configuratie
  en retentiebeleid dit toestaan.

## Canonical modeluitbreiding

- [x] Voeg aan raw én canonical transacties een open provider-metadata-contract
  toe met schema-/versienummer, bronobjecttype en geselecteerde bronvelden.
  Voeg voor vaak gebruikte velden aparte canonical properties toe in plaats
  van alles uitsluitend in JSONB te bewaren.
- [x] Voeg minimaal toe: `merchant_name`, `merchant_id`, `merchant_city`,
  `merchant_country`, `counterparty_name`, `counterparty_account_reference`,
  `merchant_category_code`, `original_type`, `original_status`,
  `authorization_status`, `settlement_status` en `source_record_hash`.
- [x] Modelleer een transactieclassificatie los van het financiële type met
  `cashflow_bucket` (`expense`, `income`, `saving`, `transfer`, `neutral`),
  een optionele suggestie, confidence, classificatiebron en override-status.
- [x] Ondersteun expliciet bruto-, netto-, fee-, tax- en refundbedragen met
  afzonderlijke valuta's waar een provider die informatie levert.
- [x] Modelleer relaties naar een bronobject: payment, card payment,
  refund, chargeback, scheduled payment, batch, request, note of attachment.
  Deze relaties moeten meerdere IDs en provider-revisions kunnen bevatten.
- [x] Voeg een generiek model toe voor transactiesplitsingen met componenten,
  bedragen, valuta, percentage, categorie-suggestie, bestemming en
  provenance. Een split mag niet de oorspronkelijke brontransactie vernietigen.
- [x] Voeg een model toe voor externe annotaties zoals notes, receipts,
  attachments en links, met type, hash, MIME-type, veilige referentie,
  eigenaar, retentie en optionele destination-reference.
- [x] Behoud bestaande accountvelden en voeg optionele capabilities toe voor
  cash, kaart, credit, investment, liabilities, multi-currency en transfers.

## Connector- en providercontract

- [x] Breid het connector-SDK uit met optionele capabilities voor merchantdata,
  MCC/category-informatie, card transactions, refunds/chargebacks, notes,
  attachments, scheduled payments, recurring patterns en transfer links.
- [x] Leg per capability vast of de data volledig, partieel, incrementeel,
  historisch of alleen per detailobject beschikbaar is.
- [x] Zorg dat bunq minimaal Payment, CardPayment, counterparty/merchant,
  MCC, statusovergangen, refunds, notes, attachments en scheduled payments
  kan aanleveren wanneer de API dit toestaat. Andere connectors kunnen dezelfde
  canonical velden vullen met hun eigen equivalenten.
- [x] Bewaar originele providerwaarden naast de normalisatie. Bijvoorbeeld
  `PAYMENT`, `SDD`, `BILLING`, MCC en providerstatus mogen niet alleen worden
  vervangen door een generiek `expense`.
- [x] Voeg contractfixtures toe per capability, inclusief providers die de
  capability niet ondersteunen, lege waarden, paginering, rate limits,
  privacy-redactie en gewijzigde bronrecords.

## Categorisatie en merchantnormalisatie

- [x] Voeg een provider-onafhankelijke merchant-identiteit toe met stabiele
  sleutel, displaynaam, aliases, land, MCC's en normalisatieversies.
- [x] Ondersteun categorisatiesuggesties uit MCC, providerlabels, merchant-
  mappings, regels en optioneel AI, steeds met provenance en confidence.
- [x] Houd source suggestion, canonical suggestion, destination category en
  user override afzonderlijk bij.
- [x] Maak mappingtabellen/configuratie beschikbaar voor meerdere taxonomieën
  en bestemmingstypen. Wealthfolio-, Actual-, YNAB- en Firefly-categorie-ID's
  mogen niet hardcoded in het canonical transactiemodel staan.
- [x] Een nieuwe sync mag handmatige categorieën, splitsingen, events en
  destination overrides niet overschrijven. Alleen expliciete bronwijzigingen
  of een expliciete rebuild mogen dat doen.

## Destination-adapters

- [x] Definieer een destination-capabilitymatrix voor accounts, cash
  activities, categories, category assignments, rules, splits, events,
  notes, attachments, budgets, recurring items en reconciliation.
- [x] Laat iedere adapter de canonical transactie vertalen naar de native
  semantiek van het doelproduct. Vermijd een universele CSV-mapping die
  betekenis verliest.
- [x] Wealthfolio: ondersteun canonical cash activities, merchantmetadata,
  category assignments, splits, events, notes en het behoud van handmatige
  overrides. Gebruik de native activity types en spending-relaties.
- [x] Actual Budget: ondersteun accounts, payees, notes, categories,
  transfers, splits en budget-relevante transacties zonder budgetcategorieën
  aan andere bestemmingen op te dringen.
- [x] YNAB: ondersteun accounts, payees, categories, transfers, cleared/
  pending-status, import IDs en eventueel category assignments binnen de
  beperkingen van de YNAB API.
- [x] Firefly III: ondersteun accounts, transactions, descriptions, tags,
  categories, bills, budgets, splits en source/destination accounts volgens
  de Firefly-semantiek.
- [x] Declareer per adapter wat bidirectioneel is. Als een bestemming alleen
  kan worden geschreven, moet Finance Sync expliciet aangeven dat wijzigingen
  niet terug naar de canonical laag worden gesynchroniseerd.
- [x] Gebruik stabiele provenance- en idempotentiesleutels per bestemming en
  sla destination-object-ID's op zonder ze als bron-ID te behandelen.

## Sync-, wijzigings- en reconciliatiegedrag

- [x] Ondersteun create, update, reverse, refund, split en delete/tombstone
  als afzonderlijke lifecycle-events; verwijder bronhistorie nooit stil.
- [x] Maak onderscheid tussen broncorrecties, user overrides en
  destination-enrichment.
- [x] Zorg voor een veilige merge-strategie: bronvelden mogen worden vernieuwd,
  maar handmatige destination-categorieën, splitsingen en events blijven staan
  tenzij de gebruiker een rebuild bevestigt.
- [x] Rapporteer per sync hoeveel records nieuw, gewijzigd, onveranderd,
  geclassificeerd, ongeclassificeerd, gesplitst, overgeslagen of mislukt zijn.
- [x] Voeg reconciliation toe tussen canonical data en ieder destination:
  ontbrekende transacties, dubbele transacties, bedrag/valuta-afwijkingen,
  ontbrekende categorieën en niet-gematchte destination-objecten.

## API, UX en beheer

- [x] Expose canonical transaction detail inclusief bronmetadata,
  categorisatiesuggesties, overrides, relaties en destination-status zonder
  secrets of volledige gevoelige payloads.
- [x] Toon per connector en destination welke capabilities werkelijk
  beschikbaar zijn en welke velden ontbreken.
- [x] Voeg beheer toe voor merchant mappings, category mappings, rules,
  overridebeleid, privacy/retentie en destination sync mode.
- [x] Maak handmatige categorie-, split- en eventwijzigingen zichtbaar met
  actor, tijdstip en provenance.
- [x] Ondersteun dry-run/preview voor een nieuwe destination zodat duidelijk is
  welke categorieën, transfers, splits en accounts zouden worden aangemaakt.

## Teststrategie en acceptance criteria

- [x] Unit- en contracttests bewijzen dat iedere bestaande connector zonder
  merchant-, category-, split- of attachment-capability backwards compatible
  blijft.
- [x] Tests bewijzen dat bunq en andere providers dezelfde canonical
  transactiesemantiek produceren voor equivalente payment-, card-, refund-,
  fee-, income- en transfergevallen.
- [x] Tests bewijzen dat MCC, providerlabels, merchantnaam, notes, attachments,
  refunds, chargebacks en statusovergangen niet verloren gaan.
- [x] Tests bewijzen dat category assignments, rules, splits en events bij een
  tweede sync behouden blijven en dat user overrides voorrang houden.
- [x] Tests bewijzen bestemming-specifieke mappings voor Wealthfolio, Actual
  Budget, YNAB en Firefly III, inclusief capability gaps en rate-limit/retry-
  gedrag.
- [x] Een volledige lokale E2E-test draait per bestemming:
  `provider mock → Finance Sync → canonical datamodel → destination adapter`
  en controleert inhoud, tellingen, bedragen, categorieën en idempotentie.
- [x] Migratie-, privacy-, autorisatie-, audit-, performance- en rollbacktests
  zijn aanwezig voordat nieuwe metadata of destination writes standaard worden
  ingeschakeld.
- [x] Architectuur- en connector-documentatie beschrijven het canonical model,
  capabilitycontract, provenancebeleid, merge-strategie en voorbeeldimplementatie.

## Voorgestelde volgorde

1. Canonical metadata, merchantvelden, cashflow-classificatie en bronrelaties.
2. Bunq/card fixtures en een provider-onafhankelijke connector-capabilitytest.
3. Merchant/MCC-normalisatie en veilige category suggestion/override-opslag.
4. Wealthfolio adapter voor categorieën, splitsingen, events en behoud van
   handmatige wijzigingen.
5. Actual Budget, YNAB en Firefly III adapters met afzonderlijke contracten.
6. Notes/attachments, refunds/chargebacks en scheduled-payment enrichment.
7. Reconciliation, beheer-UX, volledige E2E-matrix en production rollout.
