---
title: "Maak CI-failures herstelbaar en bescherm main tegen ongeteste fixes"
status: in_progress
priority: 60
---

## Context

In de 24 uur tot 2026-09-09 05:30 UTC werden 35 automatische incidentissues
gesloten: 34 CI-issues en één Wealthfolio-issue. De CI-issues kwamen uit circa
17 runs; dezelfde commit veroorzaakte vaak gelijktijdig Test, Ruff, Pyright,
Integration en E2E failures. De incidentautomatisering dedupliceert per
workflow/job/branch/failure fingerprint en sluit een issue na een volgende
succesvolle run. Dit is dus vooral een failure-amplification- en
release-governanceprobleem, niet een specifiek Python 3.12-probleem.

Belangrijkste aangetroffen oorzaken:

- `scripts/key_rotation_monitoring.py` verwijderde
  `_check_key_version_downgrade` terwijl holdout-tests deze nog importeerden;
  daardoor faalden testcollectie, Integration en E2E tegelijk.
- Ruff format-checks faalden doordat gewijzigde testbestanden niet na de
  wijziging werden geformatteerd.
- Tests waren tijd- en structuurafhankelijk: een marker verwachtte een vaste
  datum en een test legde een arbitraire bronbestand-limiet van 1.205 regels op.
- Test doubles weken af van de echte SQLAlchemy API, onder andere doordat een
  fake `Session` geen `scalars()` had.
- `main` is niet beschermd; ongevalideerde, opeenvolgende restore/reconcile/
  fix-commits konden rechtstreeks naar de default branch worden gepusht.

De bestaande lokale gate `make ci-fast` bevat al de juiste snelle controles:
Ruff format, Ruff lint, Pyright en unit tests met coverage. Deze story maakt
die gate afdwingbaar, maakt de tests robuuster en voorkomt dat één defecte
refactor zich over meerdere CI-runs en issues verspreidt.

## Doel

Maak de ontwikkel- en releaseflow zodanig dat:

1. iedere codewijziging lokaal dezelfde snelle kwaliteitsgate kan draaien als
   CI;
2. incompatibele imports, formatteringsfouten, typefouten en test-doubles vóór
   push/merge worden gevonden;
3. tijd- en bestandsgrootte-afhankelijke tests geen valse regressies meer
   veroorzaken;
4. alleen een actuele, gevalideerde PR naar `main` kan worden gemerged;
5. CI-failure-issues bruikbare root-cause-informatie bevatten zonder een
   storm van duplicaten door opeenvolgende herstelpushes.

## Scope en randvoorwaarden

- Werk uitsluitend aan CI-betrouwbaarheid, testbaarheid en branch/release
  governance. Verander geen financiële domeinlogica tenzij dit noodzakelijk is
  om een foutieve test-double of testcontract te herstellen.
- Behoud Python 3.12 als expliciete CI-target.
- Behoud Ruff als formatter en lint gate, Pyright strict voor `src/` en de
  bestaande coverage-drempel van 73%, tenzij een test aantoonbaar alleen door
  een foutieve drempel faalt.
- Gebruik de bestaande `uv`-workflow en `make`-targets; introduceer geen
  tweede dependency manager of parallel kwaliteitsconfiguratie.
- Maak kleine, thematische commits/PR's. Push geen samengestelde speculative
  fix naar `main`.

## Voortgang per 2026-09-09

De repository-implementatie is gerealiseerd; de remote validatie moet nog op
een nieuwe PR-commit worden uitgevoerd. De eerdere remote HEAD (`f3a54bb`)
faalde in Lint, Test, Integration en E2E door formattering en een ontbrekende
`_check_key_version_downgrade` tijdens testcollectie. Die oorzaken zijn nu in
de werkboom hersteld. `main` is inmiddels protected en read-only geverifieerd.

Bevestigd afgerond in repository/code:

- `.pre-commit-config.yaml` bestaat, gebruikt Ruff voor `src` en `tests` en
  pint Ruff op `v0.15.22`.
- `README.md` documenteert hook-installatie, `make ci-fast`, foutdiagnose en
  lokaal draaien vóór push.
- De lokale Ruff-, Pyright-, unit- en coverage-gates zijn reproduceerbaar.
- `Quality` draait de volledige snelle gate inclusief collection-only vóór
  Integration/E2E; de zware jobs hebben een expliciete `needs: quality`.
- Concurrency, JUnit/log-artifact uploads en compacte failure summaries bestaan
  in CI; de afzonderlijke required jobnamen zijn behouden.
- Incident-fingerprints bevatten head SHA en hergebruiken een actieve issue
  voor dezelfde onderliggende workflow/job/branch/categorie over herstelpushes.
- In de huidige werkboom bestaat de downgrade-helper met tests voor strings,
  integers, booleans, ongeldige waarden, gelijke versies en downgrades.

Laatste validatie van de huidige werkboom:

| Check | Resultaat |
| --- | --- |
| `uv run pre-commit run --all-files` | geslaagd |
| `make ci-fast` | geslaagd; 3994 passed, 8 skipped |
| Coverage | 82,77% bij drempel 73% |
| `uv run pytest --collect-only -q` | geslaagd; 4225 tests verzameld |
| key-rotation tests | 20 passed |
| `git diff --check` | geslaagd |
| Integration via Docker | 193 passed |
| E2E via Docker | 32 passed |
| Remote CI op `f3a54bb` | historische failure; nieuwe PR-run nog nodig |
| Branch protection API voor `main` | geconfigureerd en geverifieerd |

Resterende afronding voor coding agents:

1. Maak een gefocuste branch/PR-commit met de geïmplementeerde wijzigingen;
   laat remote CI op die exacte commit draaien.
2. Controleer de required-checknamen na de eerste `Quality`-run en pas branch
   protection alleen aan als GitHub een afwijkende matrixnaam rapporteert.
3. Merge uitsluitend via de beschermde PR-flow en verifieer daarna één groene
   CI-run op `main`; sluit dan de story.

## Implementatiefasen

### Fase 1 — Baseline en reproduceerbare diagnose

- [x] Leg de huidige gate vast met `make ci-fast`.
- [x] Leg per check de exacte exitcode en eventuele bestaande failures vast;
  wijzig geen configuratie voordat de baseline bekend is.
- [x] Controleer de actieve CI-workflow, reusable setup action, `Makefile`,
  `pyproject.toml`, Pyright-configuraties en eventuele pre-commit-configuratie.
- [x] Maak een korte mapping van elke failure class naar de lokale reproduceer-
  bare command:
  - `make format-check`
  - `make lint`
  - `make type`
  - `make test-ci`
  - `uv run pytest --collect-only -q`
  - `uv run pytest -m integration`
  - `uv run pytest -m e2e`
- [x] Gebruik voor iedere volgende fase eerst de gerichte check en daarna
  `make ci-fast`; laat Integration/E2E alleen draaien wanneer de snelle gate
  groen is.

### Fase 2 — Ruff en lokale developer gate

- [x] Voeg, als nog ontbrekend, een repository-root `.pre-commit-config.yaml`
  toe met Ruff format en Ruff check op dezelfde paden als CI (`src`, `tests`).
- [x] Pin de Ruff-versie via de bestaande lockfile/configuratie zodat lokaal en
  CI dezelfde formatteringsregels gebruiken.
- [x] Voeg duidelijke Makefile-targets toe of verbeter de bestaande targets
  zodat `make ci-fast` zonder verborgen omgevingsvariabelen werkt.
- [x] Voeg documentatie toe aan `README.md` of de bestaande contributing-docs:
  installatie van hooks, `make ci-fast`, foutdiagnose en de regel dat eerst
  lokaal wordt gedraaid vóór push.
- [x] Controleer dat formattering geen gegenereerde bestanden, SDK-subprojecten
  of operationele artefacts buiten de bestaande scope raakt.

Acceptatie voor deze fase:

- [x] Een nieuw bestand met een Ruff-formatfout faalt lokaal reproduceerbaar.
- [x] `pre-commit run --all-files` en `make ci-fast` gebruiken dezelfde Ruff-
  configuratie en slagen op de repository.
- [x] Een formatteringswijziging wordt door de hook automatisch of duidelijk
  herstelbaar gemeld voordat de commit wordt gemaakt.

### Fase 3 — Contract- en importveiligheid

- [x] Voeg een snelle testcollectie-check toe aan `make ci-fast` of aan een
  aparte target die CI vóór de volledige unit-suite uitvoert:
  `uv run pytest --collect-only -q`.
- [x] Voeg een gerichte regression test toe voor de publieke/private
  key-rotation helpers die door tests worden geïmporteerd. De test moet falen
  wanneer een benodigde helper wordt verwijderd of hernoemd zonder alle
  call-sites mee te wijzigen.
- [x] Gebruik een kleine testmodule of import-check voor `scripts/` zodat
  ontbrekende symbolen vroeg worden gemeld, zonder dat alle integratietests
  eerst starten.
- [x] Herstel de key-rotation downgrade-functionaliteit en alle tests als de
  huidige branch dit nog niet volledig bevat. Test zowel numerieke strings en
  integers als ongeldige waarden, booleans, gelijke versies en echte
  downgrades.
- [x] Controleer na refactors expliciet alle imports met `rg` en draai
  collection-only vóór de volledige testsuite.

Acceptatie voor deze fase:

- [x] Een ontbrekende `_check_key_version_downgrade` stopt in de collection-
  check met een gerichte foutmelding.
- [x] De key-rotation tests, unit tests, Integration en E2E importeren dezelfde
  bestaande API en slagen op een groene branch.
- [x] Geen brede `Any`- of exception-fallback wordt toegevoegd om een
  collection- of contractfout te maskeren.

### Fase 4 — Deterministische en realistische tests

- [x] Vervang hard-coded current-date assertions door een geïnjecteerde clock,
  een gecontroleerde timestamp of een assertion die alleen de eventdatum
  valideert.
- [x] Verwijder arbitraire source-line-count assertions zoals de limiet op
  `sync/orchestrator.py`. Als omvang belangrijk is, test dan modulegrenzen,
  publieke exports of cyclomatische/architectuurregels met een expliciet
  onderhoudbaar contract.
- [x] Centraliseer tijdtest-fixtures in `tests/conftest.py` of de bestaande
  testutility en documenteer timezone/UTC-semantiek.
- [x] Breng SQLAlchemy test doubles in lijn met de echte async session API:
  ondersteun minstens de methodes die production code werkelijk aanroept,
  waaronder `execute`, `scalars`, context management en relevante result
  objects.
- [x] Gebruik waar passend echte lightweight SQLAlchemy sessions in plaats van
  `SimpleNamespace`-objecten voor persistence-/reconciliationtests.
- [x] Houd assertions gedragsgericht: status, persisted values, emitted events
  en foutcontracten; niet implementatiedetails zoals regelposities.
- [x] Voeg regressietests toe voor de eerder geziene gevallen: ontbrekende ISIN,
  failed/completed sync-statussen, orphan export recovery en key-rotation
  marker-deduplicatie.

Acceptatie voor deze fase:

- [x] De unit-suite geeft op gecontroleerde UTC-timestamps hetzelfde resultaat.
- [x] De test-suite faalt niet alleen omdat Ruff een multiline-expressie anders
  formatteert.
- [x] De relevante reconciliation-tests gebruiken geen fake session die een production call stilzwijgend
  overslaat of onverwacht `AttributeError` veroorzaakt.
- [x] De coverage blijft minimaal 73% zonder nieuwe uitsluitingen die alleen
  de gate omzeilen.

### Fase 5 — CI-workflow en failure feedback

- [x] Voeg een expliciete snelle `quality` job of equivalent toe die
  `make ci-fast` één keer uitvoert vóór zware Integration/E2E jobs.
- [x] Laat zware jobs alleen starten wanneer de snelle gate geslaagd is, waar
  dit verenigbaar is met de gewenste diagnostiek. Gebruik `if: needs.quality.result
  == 'success'` of de bestaande workflowstructuur.
- [ ] Behoud afzonderlijke jobnamen voor required checks; wijzig namen alleen
  met gelijktijdige branch-protection-update.
- [x] Voeg aan falende testjobs een compacte failure summary toe met de eerste
  root-cause-regel, failing testnaam en commit SHA. Upload JUnit/log-artifacts
  ook bij collection failures.
- [x] Controleer dat de bestaande concurrency-groep obsolete runs annuleert en
  dat pushes naar dezelfde ref niet onnodig meerdere volledige pipelines
  parallel laten uitwerken.
- [ ] Verbeter de incidentissue-automatisering waar mogelijk:
  - [x] dedupliceer op workflow/job/branch/fingerprint én head SHA;
  - [x] vermeld de eerste failing step en eventuele collection failure;
  - [x] maak duidelijk wanneer een issue door een latere groene run automatisch is
    gesloten;
  - [x] voorkom een nieuw issue per herstelpush als dezelfde onderliggende
    fingerprint nog actief is.

Acceptatie voor deze fase:

- [x] Een Ruff/import/type failure is zichtbaar als één snelle, actiegerichte
  failure voordat zware jobs starten.
- [x] Eenzelfde defect op één commit maakt geen nieuwe issue voor elk afgeleid
  jobresultaat tenzij die job werkelijk een andere root cause heeft.
- [x] Logs bevatten voldoende informatie om zonder artifact-download de eerste
  defecte test/import/regel te vinden.

### Fase 6 — `main` branch protection en releaseproces

- [x] Configureer `main` als protected branch via repository settings of de
  GitHub API.
- [x] Vereis een pull request en minimaal één review voor wijzigingen naar
  `main`, tenzij het project expliciet een uitzondering documenteert.
- [x] Vereis de bestaande CI-checks met stabiele jobnamen: Lint, Type check,
  Test, Migrations, Integration, E2E en relevante security/build gates.
- [x] Schakel stale approvals uit of vereist goedkeuring van de laatste push,
  zodat nieuwe herstelcommits opnieuw worden gevalideerd.
- [x] Blokkeer directe pushes voor normale gebruikers en documenteer eventuele
  break-glass procedure voor echte incidenten.
- [x] Laat de strikte branch-up-to-date-eis als equivalent de PR opnieuw testen op de actuele
  `main`-basis wanneer meerdere PR's tegelijk klaarstaan.
- [x] Controleer branch protection met een read-only API-call en leg de
  geconfigureerde required checks vast in de repository-documentatie.

Acceptatie voor deze fase:

- [x] Een bewust falende PR kan niet naar `main` worden gemerged.
- [x] Een nieuwe commit op een goedgekeurde PR maakt de oude goedkeuring/CI-
  status ongeldig waar dat nodig is.
- [ ] Een actuele groene PR kan automatisch worden gemerged volgens de bestaande
  backlog-pipeline.
- [x] De repository retourneert niet langer `Branch not protected` voor `main`.

## Verificatieprotocol voor de coding agent

De agent mag de story pas als `done` markeren wanneer alle onderstaande checks
op de uiteindelijke commit zijn uitgevoerd en vastgelegd:

```bash
uv sync --extra dev
pre-commit run --all-files
make ci-fast
uv run pytest --collect-only -q
```

Als Docker beschikbaar is:

```bash
uv run pytest -m integration -v
uv run pytest -m e2e -v
```

Daarna:

- [x] `git diff --check` is groen.
- [ ] `git status --short` bevat alleen bedoelde wijzigingen.
- [x] Er zijn geen nieuwe `Any`, brede exception-catches, test skips of
  coverage-exclusions toegevoegd om CI groen te maken.
- [x] De PR beschrijft oorzaak, gewijzigde contracten, tests en eventuele
  GitHub-settings die buiten de repository zijn aangepast.
- [ ] De volledige remote CI-run op de PR-commit is groen.
- [ ] Na merge is één nieuwe CI-run op `main` groen en zijn er geen nieuwe CI-
  incidentissues gedurende de bestaande observatieperiode.

## Out of scope

- Het verhogen of verlagen van de coverage-drempel zonder meetbare
  kwaliteitsonderbouwing.
- Het uitschakelen van Ruff, Pyright, Python 3.12, Integration of E2E om
  failures te verbergen.
- Het herschrijven van de CI-incidentautomatisering naar een nieuw systeem.
- Productfunctionaliteit voor Wealthfolio of key management buiten de
  regressies die nodig zijn voor betrouwbare tests.

## Referenties

- [CI issue-overzicht](https://github.com/rbnbrls/finance-sync/issues?q=is%3Aissue%20state%3Aclosed)
- [Voorbeeld van test/import cascade: issue #787](https://github.com/rbnbrls/finance-sync/issues/787)
- [Voorbeeld van Ruff failure: issue #788](https://github.com/rbnbrls/finance-sync/issues/788)
- [Voorbeeld van multi-job cascade: issues #789 en #790](https://github.com/rbnbrls/finance-sync/issues/789)
- [Voorbeeld van date/line-budget failures: issue #793](https://github.com/rbnbrls/finance-sync/issues/793)
- [GitHub protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-branches/about-protected-branches)
- [GitHub Actions concurrency](https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency)
