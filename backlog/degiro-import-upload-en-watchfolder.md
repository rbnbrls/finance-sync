---
title: "Bied veilige upload en watchfolder-automatisering voor DEGIRO-exports"
status: done
priority: 28
---

## Context

De DEGIRO-pensioenconnector mag niet zelf op het handelsplatform inloggen. De
gebruiker moet officiële exports daarom eenvoudig via het control panel kunnen
uploaden of, voor een self-hosted installatie, in een lokale importdirectory
kunnen plaatsen. finance-sync moet bestanden daarna veilig, idempotent en
controleerbaar verwerken zonder financiële exports onnodig te bewaren.

De huidige worker reconstrueert connectorconfiguraties alleen uit versleutelde
credentials en geeft opgeslagen niet-geheime opties niet door. File-based
connectors kunnen daardoor niet betrouwbaar via de normale scheduler draaien.

## Dependencies

Vereist `degiro-pensioen-csv-importconnector.md`.

## Acceptance criteria

- [x] Het connector-control-panel biedt voor `degiro_pension` één duidelijke
  importflow waarin de gebruiker een transactieoverzicht, rekeningoverzicht en
  portefeuillesnapshot afzonderlijk of samen kan selecteren en vóór verwerking
  het herkende rapporttype, rekeninglabel, periode en aantal regels ziet.
- [x] Een tenant-scoped multipart upload-API accepteert uitsluitend de
  ondersteunde CSV/Excel-bestanden, hanteert configureerbare bestand- en
  rijlimieten, valideert inhoud in plaats van alleen extensie/MIME-type en
  voorkomt path traversal, formule-injectie en decompression-/parsermisbruik.
- [x] Uploads worden gestreamd naar een afgeschermde tijdelijke locatie,
  atomair verwerkt en standaard verwijderd na succes of fout. Een optionele
  retentie-instelling is expliciet, versleuteld-at-rest en zichtbaar voor de
  beheerder.
- [x] Iedere poging krijgt een `ImportRun` met tenant, connection/account,
  rapporttypen, bestandscontent-hashes, status, perioden, aantallen
  created/updated/skipped/rejected, waarschuwingen en geschoonde foutdetails;
  bestandsinhoud en persoonlijke financiële waarden staan niet in logs.
- [x] Een preview/dry-run schrijft niets en toont mappings, onbekende
  transactietypen, unresolved securities en mogelijke dubbelen. De gebruiker
  kan daarna exact die gevalideerde import bevestigen zonder een TOCTOU-wissel
  van bestanden.
- [x] Voor self-hosting kan per connectorconfiguratie een inkomende watchfolder
  worden ingesteld. De worker claimt alleen volledig geschreven bestanden,
  verwerkt batches atomair en verplaatst ze na afloop naar configureerbare
  `archive`- of `quarantine`-directories met botsingsvrije namen.
- [x] De scheduler laadt zowel credentials als opgeslagen connectoropties en
  ondersteunt connectors zonder secrets. Deze wijziging is backwards
  compatible met bunq, Trading212, YNAB en bestaande file-connectors.
- [x] Een identieke bestandshash wordt niet opnieuw verwerkt tenzij een admin
  expliciet een re-import start. Overlappende exports met andere hashes blijven
  door de transaction/holding-idempotentie veilig.
- [x] Eén fout bestand blokkeert geen latere geldige imports en veroorzaakt geen
  retry-storm. Quarantine, herproberen en definitief verwijderen zijn expliciete
  adminacties met een auditrecord.
- [x] API en UI tonen laatste succesvolle snapshot, dataperiode, freshness,
  waarschuwingen en ontbrekende rapporttypen, zonder lokale serverpaden of
  data van andere tenants te lekken.
- [x] Unit-, API-, worker- en integratietests dekken upload, dry-run/confirm,
  limieten, ongeldige bestanden, tijdelijke cleanup, watchfolder-races,
  duplicate hashes, quarantine/retry, connectors zonder credentials en
  tenant-isolatie.
- [x] OpenAPI-, beheer- en deploymentdocumentatie beschrijven uploads,
  volume-mounts, directorypermissies, retentie, backup/privacy en herstel na
  een mislukte import.
