---
title: "Lever earnings-, fundamentals- en dividendinzichten per holding"
status: todo
priority: 12
---

## Context

Wealthfolio Connect Plus belooft earnings-, fundamentals- en
dividendinzichten. finance-sync bewaart al enkele point-in-time fundamentals en
dividendtransacties, maar heeft nog geen historische vergelijkingen,
verwachting-versus-realisatie, dividendontwikkeling of portfolio-impact.

Berekeningen moeten deterministisch en testbaar in finance-sync plaatsvinden.
De lokale Hermes-installatie mag de resultaten begrijpelijk formuleren, maar
mag geen bedragen, percentages of datums zelf verzinnen of herberekenen.

## Acceptance criteria

- [ ] Het datamodel ondersteunt historische quarterly/annual earnings,
  omzet/EPS actuals en estimates, rapportageperiode, valuta, filing/publication
  date en restatements met volledige bronprovenance.
- [ ] Fundamentals omvatten minimaal de al aanwezige ratio's plus omzetgroei,
  winstgroei, marges, vrije kasstroom en schuldmaatstaven wanneer de bron die
  levert; ontbrekende waarden blijven expliciet `null` en worden niet geschat.
- [ ] Dividendinzichten tonen ontvangen dividend, trailing twaalf maanden,
  forward indicated income, yield-on-cost, groei, betaalfrequentie en komende
  ex-/betaaldatums, met correcte valutaomrekening en bron/freshness.
- [ ] Earnings-inzichten berekenen actual-versus-consensus, year-over-year
  verandering en de bijdrage aan de portfolio. Alle formules, eenheden en
  gebruikte observatie-ID's zijn via de API uitlegbaar.
- [ ] Wijzigingen en anomalieën worden alleen gemarkeerd bij configureerbare,
  gedocumenteerde drempels; stale of onvolledige data krijgt een waarschuwing
  en veroorzaakt geen stellige conclusie.
- [ ] REST- en MCP-endpoints leveren detail per security en een geaggregeerd
  portfolio-overzicht met periode-, account- en valutafilters.
- [ ] Hermes genereert uitsluitend een korte uitleg op basis van een
  gestructureerd insight-payload en citeert de gebruikte finance-sync
  observaties en externe bronnen.
- [ ] De control panel en Wealthfolio companion view tonen komende earnings,
  recente surprises, fundamentele trends en dividendinkomen met duidelijke
  `as of`-tijdstippen.
- [ ] Unit- en golden-fixturetests dekken formules, valuta, restatements,
  ontbrekende data, stale data, stock/cash dividends, tenant-isolatie en
  reproduceerbare Hermes-input.
- [ ] Documentatie vermeldt dat dit informatie en analyse is, geen
  beleggingsadvies, en beschrijft formules, bronnen en dekkingsbeperkingen.

