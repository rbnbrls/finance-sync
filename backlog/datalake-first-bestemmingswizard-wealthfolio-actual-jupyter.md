---
title: "Vervang statische exporters door een datalake-first bestemmingswizard"
status: done
priority: 27
---

## Context

finance-sync is een persoonlijke, self-hosted applicatie voor precies één
eigenaar. De centrale, genormaliseerde ledger- en portefeuilledatabase is het
enige systeem van record: de gebruiker koppelt hierin al zijn individuele
banken, brokers en handmatige imports. Andere financiële analysetools lezen
vanuit of ontvangen een afgeleide van deze datalake; zij zijn optionele
consumenten en mogen nooit vereist zijn om data te verzamelen, bewaren of te
raadplegen.

De huidige Wealthfolio- en Actual Budget-exporters worden hoofdzakelijk via
deployment-environmentvariabelen ingesteld en de control panel biedt slechts
een technische configuratieweergave en een losse uitvoerknop. Vervang die
ervaring door één stap-voor-stap bestemmingswizard. De wizard maakt een
optionele verbinding met een self-hosted Wealthfolio- of Actual Budget-server,
of richt een Jupyter-notebook als read-only datalakeconsument in. Geen van
deze bestemmingen is verplicht, en meer dan één bestemming mag tegelijk actief
zijn.

De bestaande Wealthfolio- en Actual Budget-mappers/deliverylogica mogen intern
blijven bestaan als adapters, maar hun gebruikersconfiguratie, secrets en
planning worden volledig vanuit opgeslagen bestemmingen beheerd, niet vanuit
globale `WEALTHFOLIO_*`- of `ACTUAL_BUDGET_*`-instellingen.

## UX ontwerp

- De navigatie-item **Exporters** wordt **Bestemmingen**. De lege toestand
  vertelt expliciet dat de persoonlijke datalake volledig werkt zonder een
  gekoppelde app en biedt **Bestemming toevoegen**.
- De wizard bestaat uit vier duidelijke stappen met voortgang, Terug,
  Annuleren en veilig opslaan als concept:
  1. **Kies bestemming** — kaarten voor Wealthfolio, Actual Budget en Jupyter
     Notebook, met doel, ondersteunde data en privacy-/netwerkvereisten.
  2. **Verbind** — self-hosted basis-URL, benodigde aanmeldmethode en secret;
     een expliciete **Verbinding testen** valideert bereikbaarheid,
     authenticatie en compatibiliteit vóór doorgaan.
  3. **Kies data** — selecteer individuele finance-sync-accounts en relevante
     datasets. De wizard toont een preview van accountmapping, aantallen en
     waarschuwingen; de gebruiker bevestigt het import-/exporteffect.
  4. **Activeer** — kies handmatig of een bestaand export-schema, bekijk de
     eerste geplande run en bevestig. De samenvatting zegt duidelijk dat de
     finance-sync-datalake de bron blijft.
- Na voltooiing toont iedere bestemmingskaart status, laatste/volgende run,
  gekozen accounts, gezondheid en acties **Sync nu**, **Bewerken**, **Pauzeren**
  en **Verwijderen**. Fouten bevatten een bruikbare herstelactie en geen
  credential- of financiële payloads.
- De Jupyter-variant vraagt niet om een Jupyter-server. Zij maakt een
  least-privilege, read-only datalakeconsument met een downloadbare starter
  notebook en configuratie-instructies. De notebook leest via de stabiele
  finance-sync data-API of een versieerbaar lokaal snapshotformaat, nooit via
  directe schrijf- of databasecredentials.
- Het scherm is mobiel bruikbaar, volledig met toetsenbord bedienbaar,
  toegankelijk gelabeld en communiceert alle laad-, validatie-, succes- en
  foutstatussen met tekst, niet alleen kleur of toasts.

## Acceptance criteria

- [x] finance-sync documenteert en handhaaft een single-owner productmodel:
  één lokale eigenaar en één persoonlijke datalake per installatie. De
  bestaande technische tenantgrens mag intern behouden blijven voor isolatie,
  maar onboarding, UI, documentatie en nieuwe configuratie veronderstellen
  geen huishouden, team, uitnodigingen of gedeelde externe bestemming.
- [x] De canonieke finance-sync-database blijft de bron van waarheid voor alle
  gekoppelde banken, brokers, officiële imports, accounts, transacties,
  holdings, securities en provenance. Een ontbrekende, gepauzeerde, verwijderde
  of falende bestemming verhindert ingestie, read-API's, backups of andere
  consumenten niet en kan geen canonieke brondata wijzigen.
- [x] Er is een tenant-/installatie-scoped persistent bestemmingsmodel met een
  uniek doel-ID, type (`wealthfolio`, `actual-budget`, `jupyter`), naam,
  status, geselecteerde account-/dataset-scope, versies, health- en
  runmetadata, configuratie zonder secrets en een koppeling met het bestaande
  exportschema. Meer dan één actieve bestemming van elk type is ondersteund.
- [x] Gevoelige verbindingsgegevens (zoals Actual Budget-server- of
  encryptiewachtwoord, token en Wealthfolio-credential) worden uitsluitend
  via de bestaande envelopversleuteling opgeslagen, alleen bij uitvoering
  ontsleuteld en nooit teruggegeven door API, UI, logs, metrics, auditregels,
  back-ups in plaintext of foutmeldingen. De wizard laat een bestaande secret
  nooit opnieuw zien.
- [x] De nieuwe Bestemmingen-pagina vervangt de technische exporterconfiguratie
  en implementeert de beschreven vierstapswizard, inclusief opslaan als concept,
  hervatten/annuleren, veldvalidatie, testverbinding, mapping-/datapreview,
  expliciete activatie en een beheeroverzicht met veilige actiekoppen.
- [x] De Wealthfolio-stappen ondersteunen de actuele gedocumenteerde,
  self-hosted authenticatiemethode(n), valideren alleen toegestane HTTPS of
  expliciet lokale/private HTTP-eindpunten, testen servercompatibiliteit en
  laten de gebruiker accounts en ondersteunde transacties/holdings kiezen.
  Een succesvolle test maakt nog geen remote account, activiteit of holding.
- [x] De Actual Budget-stappen ondersteunen self-hosted server-URL,
  serverwachtwoord, optionele budget-encryptiewachtwoord en het kiezen van een
  gevonden budget/sync-ID. Zij testen de verbinding en tonen vóór activatie hoe
  finance-sync-accounts worden gekoppeld of aangemaakt en of ze on-/off-budget
  zijn; een test of preview schrijft niets naar Actual Budget.
- [x] Bij activatie gebruikt iedere appbestemming de bestaande, contractgeteste
  mapper als adapter en krijgt een eigen replay-veilige deliverycursor. Een
  eerste synchronisatie en iedere herhaling zijn idempotent; mislukte exports
  zijn per bestemming veilig te hervatten zonder duplicaten of verlies in een
  andere bestemming.
- [x] Een Jupyter-bestemming creëert een afzonderlijke read-only consumer met
  roteerbare, beperkte API-credential en een downloadbare, versieerbare starter
  notebook. Deze kan minimaal accounts, transacties, holdings, securities,
  prijzen en tijdstempels/provenance ophalen in een gedocumenteerd schema en
  bevat geen finance-sync-admin-, schrijf- of databasecredentials. Een lokaal
  Parquet/Arrow- of JSON-snapshotcontract is toegestaan als het versieerbaar,
  scope-beperkt, atomair gepubliceerd en identiek documenteert is.
- [x] Voor toekomstige analyse-apps bestaat een gedocumenteerd,
  providerneutraal bestemmings-/consumercontract met capability discovery,
  versiebeheer, healthchecks, veilige credentialopslag, per-bestemming
  accountscope en replay-idempotentie. Het toevoegen van een nieuwe consumer
  vereist geen wijziging van canonieke ingestie of van bestaande bestemmingen.
- [x] Handmatig uitvoeren, pauzeren, verwijderen en schemawijzigingen werken
  per bestemming. Verwijderen stopt nieuwe levering onmiddellijk, herroept
  Jupyter-credentials en biedt vóór externe cleanup een expliciete preview en
  bevestiging; canonieke datalakedata en andere bestemmingen blijven intact.
- [x] De bestaande globale exporterinstellingen worden gemigreerd naar één
  gelijkwaardige opgeslagen concept- of actieve bestemming wanneer mogelijk.
  Daarna zijn zij alleen backwards-compatible deploymentdefaults/deprecations,
  niet zichtbaar als bewerkbare gebruikersconfiguratie en veroorzaken zij geen
  dubbele exports. De oude exporter-API en CLI blijven gedurende een
  gedocumenteerde overgang compatibel of verwijzen met een duidelijke migratie-
  fout naar de bestemming.
- [x] API-, migratie-, adapter-, scheduler-, security- en end-to-end/UI-tests
  dekken een installatie zonder bestemming, meerdere gelijktijdige
  bestemmingen, concepten, alle wizardstappen, test-voor-schrijfgedrag,
  credentialredactie, URL/TLS-validatie, accountscope, idempotente retry,
  pauzeren/verwijderen, Jupyter least privilege, datalake-onafhankelijkheid en
  backwards-compatible migratie vanuit de huidige exporterinstellingen.
- [x] README, OpenAPI, beheer- en deploymentdocumentatie beschrijven de
  single-owner/datalake-first architectuur, alle drie wizardroutes,
  self-hosted netwerk- en TLS-vereisten, secretrotatie, accountselectie,
  schema's, Jupyter-notebookgebruik, migratie van omgevingsvariabelen,
  herstel na storingen en de expliciete garantie dat Wealthfolio en Actual
  Budget optionele consumenten zijn.
