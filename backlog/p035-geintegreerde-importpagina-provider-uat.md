---
title: "Rond de geïntegreerde importpagina af met provider-UAT"
status: in-progress
priority: 35
---

# Implementatieplan — Importers en uploads samenvoegen tot één importpagina

## Status

In uitvoering. Werkpakket 1 is gestart en de eerste UI-slice is afgerond:
**Importeren** is de primaire gebruikersingang; de bestaande Importers-sectie
blijft voorlopig bereikbaar als geavanceerd beheer en als compatibiliteitslaag.

## Actuele status en openstaande punten

De backend-, catalogus-, dispatch- en statische UI-slices zijn gerealiseerd.
Open staat uitsluitend de gebruikersgerichte verificatie en het gecontroleerd
afbouwen van compatibiliteitscode:

- [ ] Doorloop browser/UAT voor DEGIRO (drie rapportrollen), Saxo, CSV,
  expenses, API-connectie, profielbewerking en accountselectie.
- [ ] Controleer retry, duplicate-confirm, tenantisolatie, permissies,
  keyboard focus, schermlezerlabels, mobiele layout en foutstatussen.
- [ ] Bepaal op basis van embedded-clientgebruik of resterende globale
  legacyfuncties weg kunnen; documenteer anders hun contract en voeg tests toe.
- [ ] Voeg redacted UAT-evidence en de laatste volledige testuitslag toe; zet
  daarna frontmatter `status` op `done`.

### Afgerond in deze uitvoersessie

- De zichtbare `Importers`-navigatielink is verwijderd.
- De uploadpagina heet nu `Importeren`.
- `Bestaande koppelingen beheren` is als secundaire actie toegevoegd.
- De bestaande `section-connectors` is bewust behouden voor de migratiefase.
- `tests/test_gui_dashboard.py`: de UI-regressiesuite is uitgebreid en slaagt.
- Connectors publiceren `ingestion_methods` en `import_wizard` als
  secret-safe catalogusmetadata.
- API-schema’s voor `/connectors` en `/connectors/catalog` leveren deze
  metadata.
- DEGIRO, SaxoInvestor, CSV import en handmatige expenses zijn als file-only
  connectors gemarkeerd; oudere connectors houden standaard API-only gedrag.
- De uploadhistorie haalt de provider uit de tenant-scoped credentialrelatie
  in plaats van iedere run als DEGIRO te rapporteren.
- De losse Saxo-uploadactie op connection-cards is verwijderd.
- De centrale wizard gebruikt `/connectors/file-uploads/dispatch` voor alle
  file-imports; een confirm-endpoint houdt DEGIRO-preview/confirm uniform.
- Saxo- en generieke file-imports registreren nu eveneens een tenant-scoped
  `ImportRun` met batchhash, bestanden, status, aantallen en foutdetails.
- De gecombineerde uploadhistorie projecteert providerinformatie nu correct
  uit de tenant-scoped credentialrelatie; hiervoor is een regressietest
  toegevoegd.
- De API-wizard test nieuwe credentials vóór opslaan en biedt daarna
  accountselectie aan via hetzelfde accountcontract als de bestaande
  configuratiewizard.
- CSV- en expenses-wizards voeren vóór dispatch respectievelijk automatische
  kolommapping- en JSON-structuurvalidatie uit.
- De uploadhistorie bevat nu profiel, periode, regels, skipped/rejected,
  warnings, foutdetails, pogingnummer en een opnieuw-uploadenactie.
- De gedeelde staginglaag accepteert nu de extensies die de wizard aanbiedt:
  CSV, TXT, XLSX, XLS en JSON; JSON wordt niet onnodig als spreadsheet
  behandeld.
- De oude DEGIRO-DOM-tree, de automatische legacy-initialisatie en de oude
  Saxo-bestandsinvoer in de algemene configuratiemodal zijn verwijderd. De
  legacy-functies blijven alleen nog als tijdelijke globale compatibiliteits-
  laag beschikbaar.
- Mislukte directe file-imports bewaren hun `failed`-run met geschoonde fout-
  details in plaats van deze door rollback te verliezen.
- UI/API/registry-checks: gerichte tests geslaagd; Ruff op gewijzigde
  Pythonbestanden is groen.
- Volledige projecttests: 3523 geslaagd, 193 overgeslagen. De bestaande
  waarschuwingen zijn niet door deze wijziging geïntroduceerd.
- Gerichte Pyright-check: 0 errors; gerichte Ruff-check en `git diff --check`
  zijn groen.
- Laatste volledige projecttest: 3523 geslaagd, 193 overgeslagen.
- Laatste volledige validatie na de wizard-, staging-, history- en API-account-
  wijzigingen: 3523 geslaagd, 193 overgeslagen; dashboardscript syntactisch
  gevalideerd. De centrale API-flow biedt ook accountbeheer voor bestaande
  profielen.

## Doel

Geef de gebruiker één pagina waarop eerst een tegenpartij wordt gekozen en
daarna één provider-specifieke wizard verschijnt. Die wizard ondersteunt de
beschikbare methode(n):

- API koppelen;
- bestanden uploaden;
- of beide, wanneer een tegenpartij beide mogelijkheden aanbiedt.

De bestaande connector-, sync-, preview-, confirm- en accountselectielogica
blijft functioneel. De wijziging consolideert vooral de gebruikersinterface en
maakt provider-capabilities expliciet in het connectorcataloguscontract.

## Huidige basis en scope

De unified flow bestaat al in
`src/finance_sync/templates/dashboard.html` rond `loadImportFlow()` en
`renderImportProviders()`. De volgende parallelle flows moeten daarin opgaan:

- `openCreateWizard()` / `openConfigModal()` voor Importers;
- de legacy DEGIRO-wizard;
- de oude Saxo-uploadmodal;
- de inline profielconfiguratie die nu apart naast de algemene configuratiemodal
  bestaat.

Buiten scope:

- nieuwe financiële connectors;
- wijziging van de canonical ingestie- of idempotentiestrategie;
- verwijderen van bestaande endpoints voordat de nieuwe flow aantoonbaar werkt;
- watchfolder-functionaliteit.

## Werkpakketten

### 1. Primaire gebruikersingang consolideren

**Doel:** de gebruiker ziet één duidelijke route voor nieuwe imports.

1. Maak `uploads` de primaire navigatie-ingang en label deze consequent als
   `Importeren`.
2. Verwijder de zichtbare dubbele `Importers`-navigatielink.
3. Houd connectorbeheer tijdelijk bereikbaar via een secundaire actie
   `Bestaande koppelingen beheren` op de importpagina en via bestaande
   dashboard-acties.
4. Houd de Importers-sectie intern beschikbaar zolang de migratie en regressie-
   tests niet volledig zijn afgerond.
5. Verwijder na de migratie de legacy DOM-hooks en oude wizardfuncties. De
   DOM-hooks en automatische initialisatie zijn verwijderd; alleen de globale
   functies blijven tijdelijk behouden voor mogelijke embedded clients.

**Verificatie:** navigatie, directe `switchSection()`-links, permissie-filtering
en refresh/deep-linkgedrag blijven werken.

### 2. Connectorcatalogus uitbreiden met ingestiemethoden

**Doel:** de frontend hoeft niet meer via een hardcoded `FILE_IMPORTERS`-set te
beslissen wat een provider ondersteunt.

1. Voeg aan connectorcatalogus en schema’s een secret-safe veld toe, bijvoorbeeld
   `ingestion_methods: ["api", "file"]`.
2. Voeg voor file-providers een wizarddefinitie toe met bestandstypen, verplichte
   rollen, uitleg, preview/confirm-ondersteuning en eventuele profielvereisten.
3. Modelleer minstens:
   - API: Bunq, Trading212, Plaid-like en YNAB waar configureerbaar;
   - file: DEGIRO Pensioen, SaxoInvestor, CSV import en Handmatige uitgaven.
4. Laat bestaande connectorcatalogusclients backwards compatible functioneren
   wanneer metadata ontbreekt: behandel een ontbrekende methode als `api` voor
   bestaande API-connectors en markeer onvolledige metadata.

**Verificatie:** OpenAPI-schema, cataloguscontracttests en tests per connector
voor methodes, velden en toegestane bestandstypen.

### 3. Eén gedeelde wizard-shell en renderer

**Doel:** alle providers gebruiken dezelfde navigatie-, validatie- en status-
patronen.

1. Maak één wizardstate met provider, methode, connection/profile, bestanden,
   huidige stap, preview/import-run en foutstatus.
2. Maak gedeelde stappen voor:
   - tegenpartij/methode kiezen;
   - profiel kiezen of maken;
   - credentials of bestanden verzamelen;
   - testen/previewen;
   - bevestigen en resultaat tonen.
3. Gebruik provider-metadata voor labels en uitleg; houd alleen complexe
   providerlogica (DEGIRO-rapportrollen, Saxo-posities/transacties) in kleine
   provideradapters.
4. Hergebruik dezelfde credential- en options-renderer voor een nieuwe
   verbinding en voor edit/reauthentication.
5. Zorg voor keyboard-navigatie, correcte focus na stapwissels, loading/error-
   states en mobiel gedrag.

**Verificatie:** browser/UAT-scenario’s voor nieuwe API-koppeling, bestaande
API-koppeling, eerste file-profiel, bestaand file-profiel, teruggaan naar een
andere tegenpartij en retry na een fout.

### 4. DEGIRO-, Saxo- en generieke fileflows migreren

**Doel:** per provider bestaat nog maar één gebruikerswizard.

1. Migreer DEGIRO naar één wizard met drie expliciete rapportrollen:
   Accountoverzicht, Transacties en Portefeuille.
2. Behoud de bestaande preview/confirm-veiligheid en batch-hash/idempotentie.
3. Migreer Saxo naar één wizard met Posities en Transacties, inclusief eerste
   profielcreatie zonder bestanden opnieuw te kiezen.
4. Laat CSV en Handmatige uitgaven een generieke filewizard gebruiken met
   respectievelijk mapping- en JSON-validatiestap.
5. Verwijder de legacy DEGIRO DOM-tree, `initDegiroWizard()` en de oude Saxo-
   modal pas nadat tests en UAT groen zijn. De DOM-tree, automatische init en
   Saxo-modal zijn verwijderd; de resterende globale DEGIRO-functies vormen
   alleen een tijdelijke compatibiliteitslaag voor embedded clients.
6. Corrigeer tekst en aantallen zodat DEGIRO overal drie bestanden vermeldt.

**Verificatie:** bestaande DEGIRO- en Saxo-tests, upload-preview/confirm,
ongeldige bestandstypen, ontbrekende rapporten, duplicate batches en cleanup.

### 5. Gemeenschappelijk importcontract achter de bestaande adapters

**Doel:** de UI kent geen provider-specifieke endpoint-URL’s meer.

1. Introduceer een dun intern dispatchcontract voor file-imports met
   `provider_type`, `connection_id`, bestanden en optionele previewmodus.
2. Routeer dit intern naar de bestaande DEGIRO-, Saxo- en generieke adapters.
3. Behoud de huidige provider-endpoints tijdelijk als backwards-compatible
   wrappers.
4. Gebruik voor preview altijd een exact bevestigbare `ImportRun`; bestanden
   mogen niet tussen preview en confirm verwisselen.
5. Laat API-koppelingen de bestaande config/test/accountselectie-contracten
   behouden.

**Verificatie:** API-contracttests, tenant-isolatie, permission matrix,
TOCTOU/duplicate-confirm-tests en backwards-compatibilitytests.

### 6. Uploadgeschiedenis en status normaliseren

**Doel:** één pagina toont alle imports correct.

1. Sla `provider_type` correct op in iedere `ImportRun` en verwijder de huidige
   hardcoded DEGIRO-waarde in `file_uploads.py`.
2. Toon provider, profiel, bestand(en), status, periode, waarschuwingen en
   created/updated/skipped counts.
3. Voeg duidelijke statussen toe voor previewed, completed, failed en retry.
4. Houd alle resultaten tenant-scoped en zonder lokale paden of gevoelige data.

**Verificatie:** historie met DEGIRO, Saxo, CSV en expenses; cross-tenant API-
tests; mislukte import en retry; lege historie.

### 7. Oude routes, UI en documentatie opruimen

1. Zoek alle verwijzingen naar `FILE_IMPORTERS`, oude file-upload-DOM-hooks,
   `openSaxoImport`, `initDegiroWizard` en dubbele profielrenderers.
2. Verwijder alleen code die niet meer door embedded clients of tests gebruikt
   wordt; pas tests en compatibiliteitsnotities tegelijk aan. De actieve
   `saveConfig()`-flow roept geen legacy DEGIRO-loader meer aan en de legacy
   wizard wordt niet meer automatisch geïnitialiseerd.
3. Werk README/API-beheerinformatie bij met de nieuwe gebruikersflow.
4. Voeg release-notes toe voor het verdwijnen van de aparte Importers-ingang;
   dit staat in `docs/RELEASE-NOTES-importeren.md`.

## Definition of Done

- Er is voor de gebruiker één primaire pagina `Importeren`.
- De gebruiker kiest eerst de tegenpartij.
- De beschikbare API- en/of filemethodes zijn zichtbaar en begrijpelijk.
- Iedere provider heeft één wizard met provider-specifieke instructies.
- API-connecties, accountselectie, file-preview, confirm en idempotentie blijven
  werken.
- DEGIRO, Saxo, CSV en expenses hebben correcte historie en status.
- Oude dubbele UI-flows zijn verwijderd of aantoonbaar alleen als tijdelijke
  compatibiliteitslaag aanwezig.
- Unit-, API-, template- en browser/UAT-tests zijn groen.

## Uitvoeringsvolgorde

1. Primaire navigatie-ingang — deels afgerond; migratie en opruimen blijven open.
2. Cataloguscontract — afgerond, inclusief provider-specifieke metadata-rendering.
3. Gedeelde wizard-shell — gereed; provider-specifieke UAT blijft open.
4. Providerflows migreren — centrale flow en dispatch gereed; provider-UAT blijft open.
5. Gemeenschappelijk importcontract — dispatch gereed; bestaande adapters blijven backwards-compatible.
6. Historie/status corrigeren — providerprojectie en `ImportRun`-registratie
   voor alle file-adapters gereed; retry-/failure-UAT blijft open.
7. Legacy opruimen, volledige test- en UAT-gate — DOM en actieve legacy-flow
   zijn opgeruimd; alleen globale compatibiliteitsfuncties blijven aanwezig.
   Browser-UAT blijft het enige open verificatiepunt omdat de lokale
   browserruntime ontbreekt; de volledige geautomatiseerde test- en statische
   validatie is groen.
