---
title: "Voeg een DEGIRO-pensioenconnector voor officiële exports toe"
status: done
priority: 29
---

## Context

DEGIRO biedt geen publieke API aan en geeft aan dat externe API-wrappers en
scripts die op het handelsplatform inloggen niet worden ondersteund en in
strijd zijn met de voorwaarden. Een live connector met gebruikersnaam,
wachtwoord, 2FA of private endpoints valt daarom buiten scope.

DEGIRO laat gebruikers wel officieel transactieoverzichten,
rekeningoverzichten en portefeuillesnapshots als CSV of Excel exporteren. Een
read-only importconnector kan hiermee de afzonderlijke pensioenrekening als
fiscaal geblokkeerde beleggingsrekening in finance-sync opnemen zonder
DEGIRO-inloggegevens te bewaren.

## Dependencies

Vereist `connector-holdings-security-en-fx-ingestie.md`.

## Acceptance criteria

- [x] Er is een ingebouwde connector met provider key `degiro_pension`,
  displaynaam `DEGIRO Pensioen` en capabilities voor accounts, transacties en
  holdings; registratie, connectoroverzicht en documentatie zijn bijgewerkt.
- [x] De connector gebruikt uitsluitend door de gebruiker aangeleverde
  officiële exports en bevat geen HTTP-client, loginflow, cookie-/sessiebeheer,
  2FA-secret, browserautomatisering of calls naar private DEGIRO-endpoints.
- [x] De connector accepteert CSV en de actuele Excel-exportformaten voor
  transactieoverzicht, rekeningoverzicht en portefeuilleoverzicht. PDF is
  expliciet niet ondersteund en levert een duidelijke, niet-technische fout.
- [x] Het rapporttype wordt veilig uit headers en structuur herkend. Bekende
  Nederlandse en Engelse kolomnamen en de gangbare 12-, 14- en 18-koloms
  DEGIRO-varianten worden ondersteund zonder op de bestandsnaam te vertrouwen.
- [x] Het transactieoverzicht mapt aankopen en verkopen met datum/tijd, order-ID,
  ISIN, product, venue, quantity, prijs, instrumentvaluta, EUR-waarde,
  wisselkoers en transactiekosten naar canonical data.
- [x] Het rekeningoverzicht mapt minimaal stortingen, opnames, dividend,
  dividendbelasting, rente, platform-/aansluitingskosten, corporate-actionkosten
  en relevante valutamutaties. Cash sweeps en technische spiegelboekingen
  worden aantoonbaar niet dubbel geteld.
- [x] Het portefeuilleoverzicht maakt een holdingsnapshot met ISIN, product,
  quantity, prijs, marktwaarde, cost basis/GAK wanneer aanwezig en valuta. Cash
  wordt apart van effecten verwerkt.
- [x] De account wordt aangemaakt als `account_type=investment` en
  `account_subtype=nl_lijfrente`. De externe account-ID is stabiel per
  connectorconfiguratie en bevat standaard geen DEGIRO-gebruikersnaam of ander
  persoonlijk gegeven.
- [x] `current_balance` representeert de totale pensioenrekeningwaarde op het
  moment van de portefeuillesnapshot, inclusief correct gedocumenteerde
  cashbehandeling, zodat net worth de portefeuille niet als alleen cash telt.
- [x] Externe transactie-ID's zijn stabiele hashes van genormaliseerde
  bronvelden zoals rapporttype, order-ID, timestamp, ISIN, gebeurtenistype,
  bedrag en valuta. Ze gebruiken geen regelnummers en onderscheiden meerdere
  fills of fee-/taxregels met dezelfde order-ID.
- [x] Overlappende datumexports en het opnieuw importeren van dezelfde bestanden
  zijn idempotent. Een import met ongeldige of onverwachte regels geeft een
  bruikbaar validatierapport en wordt niet stil als volledig succesvol
  geregistreerd.
- [x] Parsing is locale- en encodingbestendig voor onder meer BOM, komma- en
  puntdecimalen, quoted komma's in productnamen en winter-/zomertijd. Geld en
  quantity worden uitsluitend met `Decimal` verwerkt.
- [x] Golden fixtures bevatten synthetische of aantoonbaar geanonimiseerde
  voorbeelden van alle drie rapporttypen, meerdere exportversies, meerdere
  valuta's, meerdere fills, dividendbelasting, cash sweeps, lege portefeuilles
  en malformed input; er komen geen echte rekeninggegevens in git.
- [x] Documentatie bevat de exacte handmatige exportstappen in DEGIRO, de drie
  benodigde bestanden, ondersteunde formaten, bekende beperkingen en de
  expliciete reden waarom finance-sync niet automatisch op DEGIRO inlogt.
