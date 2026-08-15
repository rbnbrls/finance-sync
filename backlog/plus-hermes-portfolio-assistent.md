---
title: "Bied een lokale Hermes portfolio-assistent zonder externe AI-key"
status: todo
priority: 9
---

## Context

Wealthfolio Connect Plus belooft een AI portfolio assistant waarvoor geen
eigen externe API-key nodig is. De self-hosted variant gebruikt de bestaande
lokale Hermes-installatie op Proxmox als agent en finance-sync als
tenant-scoped, controleerbare feiten- en toollaag.

De assistent moet vragen over bunq, Trading212 en de gecombineerde portfolio
kunnen beantwoorden. Hij is standaard read-only, toont bronnen en mag nooit
autonoom transacties uitvoeren of provider-/deploymentinstellingen wijzigen.

## Acceptance criteria

- [ ] Een versiebeheerbare Hermes-agentconfiguratie/system prompt gebruikt de
  finance-sync MCP-server met een afzonderlijke least-privilege API-key en
  werkt zonder `AI_API_KEY`, OpenAI-key of Anthropic-key.
- [ ] MCP-tools ontsluiten minimaal accounts, transacties, holdings,
  performance, allocation, cashflow, dividends, fundamentals, earnings,
  analistenconsensus, relevant nieuws/events, briefings en sync-freshness met
  consistente tenant- en household-authorisatie.
- [ ] De assistent kan vergelijkingen en scenario-uitleg geven, maar gebruikt
  finance-sync-services voor alle numerieke berekeningen en toont gebruikte
  periode, valuta, `as of` en fact/source-citations.
- [ ] Antwoorden onderscheiden feiten, afleidingen, ontbrekende data en
  onzekerheid. Niet-ondersteunde of stale informatie leidt tot een expliciete
  beperking in plaats van een verzonnen antwoord.
- [ ] Nieuws, documenten, transactieteksten en transcripten worden als
  onbetrouwbare input behandeld. Prompt-injection kan geen extra tools openen,
  secrets uitlezen, data van andere tenants benaderen of systeeminstructies
  wijzigen.
- [ ] Alle financiële en configuratiemutaties zijn standaard uitgeschakeld.
  Eventuele later toegestane acties gebruiken afzonderlijke scoped tools,
  previewen het effect en vereisen per actie expliciete gebruikersbevestiging;
  brokerhandel blijft altijd buiten scope.
- [ ] De control panel en een ondersteunde Wealthfolio add-on/companion panel
  bieden chat, bronlinks, conversation reset en feedback zonder ongedocumenteerde
  writes naar de Wealthfolio-database.
- [ ] Conversaties en tool-audits blijven lokaal op Proxmox, hebben
  configureerbare retentie en kunnen door de gebruiker volledig worden
  verwijderd. Logs bevatten geen volledige portfolio- of promptpayloads.
- [ ] Healthchecks tonen Hermes- en MCP-bereikbaarheid; uitval van de assistent
  heeft geen invloed op bunq-/Trading212-sync, Wealthfolio-export of bestaande
  read-API's.
- [ ] E2E-tests dekken feitelijke vragen, berekeningen, citations,
  tenant/household-isolatie, prompt-injection, tool-denial, Hermes-timeout en
  werking zonder externe AI-netwerktoegang.
- [ ] Documentatie beschrijft lokale installatie, key rotation, toegestane
  tools, privacy/retentie, incident response en dat de assistent informatie
  geeft maar geen beleggingsadvies of handelsopdrachten uitvoert.

