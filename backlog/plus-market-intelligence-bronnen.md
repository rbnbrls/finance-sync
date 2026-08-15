---
title: "Bouw een legale self-hosted bronlaag voor portfolio-intelligence"
status: todo
priority: 14
---

## Context

Wealthfolio Connect Plus noemt "licensed market intelligence" als basis voor
nieuws, events, earnings en analisteninzichten. Een gratis self-hosted variant
kan betaalde content niet zonder licentie kopiëren. finance-sync moet daarom een
provider-onafhankelijke bronlaag krijgen die open data en bronnen waarvoor de
gebruiker zelf toegangsrechten heeft combineert. Hermes mag deze gegevens later
samenvatten, maar is nooit de bron van financiële feiten.

De bestaande OpenBB-enrichment en `FundamentalObservation` vormen een begin,
maar er is nog geen uniform model voor nieuws, corporate events,
earningsmateriaal, analistenconsensus, bronrechten en citaties.

## Acceptance criteria

- [ ] Er is een providerinterface voor minimaal `news`, `corporate_events`,
  `earnings`, `analyst_estimates` en `earnings_call` met capability discovery,
  rate limits, retries, freshness en een expliciete unavailable-status.
- [ ] Er zijn werkende adapters voor de reeds geconfigureerde OpenBB-bronnen en
  minimaal één juridisch herbruikbare publieke bron voor nieuws/events. Bronnen
  waarvoor een eigen abonnement of API-key nodig is zijn optioneel en worden
  alleen actief na expliciete configuratie door de gebruiker.
- [ ] Iedere observatie bewaart bron, canonieke URL/document-ID,
  publicatietijd, ophaaltijd, geldigheidsperiode, taal, licentie/gebruiksklasse
  en een content-hash. Afgeleide records blijven naar de oorspronkelijke bron
  herleidbaar.
- [ ] finance-sync bewaart geen volledige auteursrechtelijk beschermde
  artikelen of transcripten wanneer de bronlicentie dat niet toestaat. In dat
  geval worden alleen toegestane metadata, korte snippets, gestructureerde
  feiten en een bronlink opgeslagen.
- [ ] Security-identiteit wordt via bestaande FIGI/ISIN/ticker/listing-logica
  opgelost. Ambigue matches gaan naar een reviewqueue en worden niet stil aan
  een holding gekoppeld.
- [ ] Ingestion is incrementeel en idempotent; dubbele syndicatie-items worden
  op bron-ID en content-hash gededupliceerd en providerstoringen verwijderen
  geen eerder geldige data.
- [ ] Een scheduler ververst bronnen volgens hun eigen cadence en registreert
  runs, latency, quota, freshness en geschoonde fouten. Een storing blokkeert
  bunq-, Trading212- of Wealthfolio-sync niet.
- [ ] REST- en MCP-readcontracten ontsluiten bronmetadata en gestructureerde
  feiten tenant-scoped, maar nooit providercredentials of niet-gelicentieerde
  volledige content.
- [ ] Providercredentials worden met de bestaande envelope-encryptie beheerd;
  logs, metrics, API-responses en Hermes-prompts bevatten geen secrets.
- [ ] Contract-, migratie- en integratietests dekken deduplicatie, licensing
  policy, identifier resolution, rate limiting, stale data en provider failure.
- [ ] Documentatie bevat per adapter de herkomst, licentievoorwaarden,
  configuratie, bekende dekking en een procedure om een bron direct uit te
  schakelen of diens data te verwijderen.

