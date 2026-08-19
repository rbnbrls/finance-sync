---
title: "Toon nieuws en events die relevant zijn voor actuele holdings"
status: done
priority: 13
---

## Context

Wealthfolio Connect Plus belooft nieuws en gebeurtenissen die relevant zijn
voor de holdings van de gebruiker. Na de market-intelligence-bronlaag moet
finance-sync items aan de actuele Trading212-posities koppelen en ruis
onderdrukken. bunq-cashrekeningen mogen alleen portfolio-events beïnvloeden
wanneer dat inhoudelijk relevant is, bijvoorbeeld rente- of valuta-events.

Hermes kan titels clusteren en relevantie uitleggen, maar de securitymatch,
holdingstatus, datums en bronverwijzingen blijven deterministische
finance-sync-feiten.

## Acceptance criteria

- [ ] Een relevance-service koppelt nieuws en corporate events aan actuele of
  recent verkochte holdings via canonieke security-ID's en bewaart de gebruikte
  matchreden en confidence.
- [ ] Ondersteunde events omvatten minimaal earnings-datums, dividend
  ex-/record-/betaaldatums, aandeelhoudersvergaderingen, splits, fusies,
  overnames en relevante regulatorische filings voor zover bronnen die leveren.
- [ ] De service clustert dubbele of gesyndiceerde berichten tot één verhaal,
  behoudt alle bronlinks en sorteert op holdinggewicht, eventnabijheid,
  recency en betrouwbaarheidsniveau.
- [ ] Generieke ticker- of naamsmatches met onvoldoende confidence worden niet
  getoond als holdingnieuws. Een review- en correctieflow kan false positives
  herstellen en toekomstige matching verbeteren.
- [ ] API- en MCP-endpoints ondersteunen filters op security, account,
  itemtype, datum en unread/acknowledged-status en geven altijd source URL,
  gepubliceerd/opgehaald op en freshness terug.
- [ ] Een Hermes-uitleg mag in maximaal enkele zinnen aangeven waarom een item
  relevant is, maar introduceert geen onbevestigde feiten en verwijst naar de
  onderliggende intelligence-item-ID's.
- [ ] De finance-sync control panel en een gedocumenteerde Wealthfolio
  add-on/companion view tonen een holdingfeed en kalender zonder rechtstreeks in
  de Wealthfolio SQLite-database te schrijven.
- [ ] Notificaties zijn opt-in, dedupliceren per cluster/event en lekken op het
  lockscreen standaard geen positieomvang of financiële waarde.
- [ ] Tests bewijzen correcte koppeling, ranking, deduplicatie, false-positive
  afhandeling, tenant/household-zichtbaarheid en graceful degradation bij stale
  of ontbrekende bronnen.
- [ ] Documentatie legt scoring, ondersteunde events, databronnen,
  notificatie-instellingen en beperkingen per markt uit.

