---
title: "Genereer gepersonaliseerde portfolio-briefings met lokale Hermes"
status: todo
priority: 10
---

## Context

Wealthfolio Connect Plus belooft personalized portfolio briefs. finance-sync
heeft al een eenvoudige daily briefing, maar die gebruikt een externe
OpenAI/Anthropic-compatibele API-key, alleen een eendaagse financiële snapshot
en geen marktintelligence of duurzame briefinghistorie.

Voor de self-hosted variant draait Hermes lokaal op de Proxmox-server. Hermes
wordt als MCP-client aan finance-sync gekoppeld, haalt alleen tenant-scoped,
gestructureerde context op en schrijft het resultaat via een smal,
geauthenticeerd contract terug. De gewone sync/export blijft werken wanneer
Hermes uitstaat.

## Acceptance criteria

- [ ] Er is een gedocumenteerde Hermes-agentconfiguratie die de finance-sync
  MCP-server via een least-privilege tenant API-key gebruikt; de sleutel is
  intrekbaar en geeft alleen toegang tot benodigde read-tools en het opslaan van
  briefings.
- [ ] De briefingcontext combineert holdings, performance, allocation,
  transacties/cashflow, relevante nieuws/events, earnings, fundamentals,
  dividenden en analisteninzichten met consistente `as of`-tijdstippen.
- [ ] Gebruikers kunnen frequentie, taal, lengte, valuta, accounts/portfolio's,
  focusonderwerpen en stille dagen instellen. Defaults werken zonder externe AI
  API-key.
- [ ] Hermes genereert dagelijks en on-demand een gestructureerde briefing met
  `what changed`, aankomende events, relevante risico's/afwijkingen en
  bronverwijzingen. Ongewijzigde of onvoldoende verse data wordt als zodanig
  gemeld.
- [ ] Bedragen en berekeningen worden uit finance-sync toolresultaten overgenomen;
  Hermes voert geen verborgen financiële berekeningen uit en iedere feitelijke
  claim is aan een finance-sync fact-ID of externe source-ID gekoppeld.
- [ ] Briefings worden duurzaam opgeslagen met tenant/user-scope,
  Hermes/modelversie, promptversie, input-hashes, citations, generated-at,
  status en supersession. Retentie en handmatige verwijdering zijn instelbaar.
- [ ] Een in-repo scheduler of expliciet geconfigureerde Hermes-taak start de
  generatie idempotent. Retries produceren geen dubbele briefing en een
  Hermes-timeout vertraagt geen provider- of Wealthfolio-sync.
- [ ] `get_daily_briefing` en de REST-API leveren de opgeslagen Hermes-briefing
  en een duidelijke pending/unavailable-status in plaats van een externe
  OpenAI/Anthropic-key te vereisen.
- [ ] De control panel en Wealthfolio companion view tonen briefinghistorie,
  citations, freshness en een handmatige regenerate-knop met rate limiting.
- [ ] Netwerkverificatie bewijst dat prompts en portfolio-inhoud de lokale
  Proxmox-omgeving niet verlaten; alleen expliciet geconfigureerde
  marktdatarequests zijn toegestaan.
- [ ] Tests dekken personalisatie, tenant/household-zichtbaarheid,
  idempotentie, stale inputs, ontbrekende bronnen, Hermes-uitval,
  ongeciteerde output en privacy/redaction.
- [ ] Documentatie bevat installatie, Hermes-MCP-configuratie, scheduling,
  troubleshooting, privacygrenzen en de disclaimer dat de briefing geen
  beleggingsadvies is.

