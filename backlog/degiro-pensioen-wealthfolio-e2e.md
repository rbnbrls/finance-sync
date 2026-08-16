---
title: "Lever DEGIRO-pensioendata end-to-end aan Wealthfolio"
status: todo
priority: 27
---

## Context

Na het importeren van officiële DEGIRO-exports moeten de pensioenrekening,
activiteiten en actuele posities correct in de self-hosted Wealthfolio-instance
verschijnen. Alleen transacties doorsturen is onvoldoende: de actuele holdings,
totale waarde, dividendbelasting, valuta en security-identiteit moeten
reconcilieerbaar blijven. Herhaalde en overlappende imports mogen geen dubbele
Wealthfolio-activiteiten of posities veroorzaken.

## Dependencies

Vereist `connector-holdings-security-en-fx-ingestie.md`,
`degiro-pensioen-csv-importconnector.md` en bij voorkeur
`degiro-import-upload-en-watchfolder.md` voor de productieflow.

## Acceptance criteria

- [ ] De Wealthfolio-exporter maakt of koppelt één herkenbaar
  `DEGIRO Pensioen`-account per finance-sync-verbinding en houdt dit gescheiden
  van een eventuele gewone DEGIRO-, bunq- of Trading212-rekening.
- [ ] Aankopen, verkopen, stortingen, opnames, dividend, dividendbelasting,
  rente en kosten worden naar de juiste Wealthfolio-activitytypes gemapt met
  correcte tekenconventie, quantity, unit price, fee, valuta, FX-rate en datum.
- [ ] Iedere security gebruikt de door finance-sync opgeloste listing en bij
  voorkeur ISIN; een ambigue of unresolved security wordt niet stil met een
  gelijkende ticker geëxporteerd en verschijnt in een herstelbare reviewflow.
- [ ] De exporter ondersteunt een gedocumenteerde activity-first strategie voor
  historie en een holdingsnapshot voor bootstrap/reconciliatie, zonder dezelfde
  portefeuillewaarde dubbel in Wealthfolio op te nemen.
- [ ] Dividendbelasting blijft afzonderlijk herkenbaar als `TAX` en wordt niet
  bij dividend opgeteld of als generieke transactiekosten verborgen.
- [ ] Multi-currency activiteiten gebruiken de originele instrument-/cashvaluta
  en de beschikbare DEGIRO-wisselkoers; EUR-basisbedragen sluiten binnen een
  gedocumenteerde afrondingstolerantie aan op de bronexport.
- [ ] De finance-sync rekeningwaarde, som van holdings plus cash en de waarde in
  Wealthfolio worden na iedere volledige import automatisch gereconcilieerd.
  Afwijkingen boven configureerbare absolute of procentuele toleranties leveren
  een zichtbare finding op en markeren de export niet stil als gezond.
- [ ] Twee identieke volledige imports en twee gedeeltelijk overlappende
  datumexports leveren in Wealthfolio exact één activiteit per economische
  gebeurtenis en één actuele positie per security op.
- [ ] Correcties of nieuwere snapshots superseden eerdere data volgens een
  gedocumenteerd, niet-destructief beleid. Verwijderen of vervangen van reeds
  geëxporteerde Wealthfolio-data vereist een preview en expliciete bevestiging.
- [ ] De scheduler kan na een succesvolle DEGIRO-import alleen de betrokken
  account/exportcursor hervatten. Een Wealthfolio-storing verandert de
  geslaagde bronimport niet en is veilig opnieuw te proberen.
- [ ] Een end-to-end fixturetest bewijst de route `DEGIRO exports -> parser ->
  canonical account/transactions/holdings/securities -> Wealthfolio payload`
  voor aankopen, verkoop, dividend, belasting, kosten, FX en actuele holdings.
- [ ] Een productie-smoke-run tegen de geconfigureerde self-hosted
  Wealthfolio-instance controleert accountzichtbaarheid, aantallen,
  portefeuillewaarde en idempotentie zonder echte financiële waarden of
  credentials in CI-logs of PR-artifacts te publiceren.
- [ ] Gebruikersdocumentatie beschrijft eerste import, periodiek bijwerken,
  freshness, reconciliatiefouten, unresolved securities, herstel en dat het om
  een handmatige-exportkoppeling gaat en niet om een live DEGIRO-API.

