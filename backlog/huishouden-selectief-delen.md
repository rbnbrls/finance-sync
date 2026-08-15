---
title: "Voeg een gedeeld huishouden met selectieve accountdeling toe"
status: todo
priority: 15
---

## Context

Wealthfolio Connect Duo biedt twee personen een gedeeld huishoudbeeld waarbij
alleen gekozen gegevens worden gedeeld. finance-sync heeft tenants, gebruikers
en rollen, maar alle gebruikers in een tenant zien momenteel dezelfde
tenantdata. Accounts hebben geen eigenaar of deelbeleid en de exporter kan geen
onderscheid maken tussen privé- en gedeelde accounts.

De self-hosted variant moet minimaal twee huishoudleden ondersteunen zonder
betaalplan of kunstmatige limiet. Ieder lid behoudt een privébeeld; alleen
expliciet gedeelde bunq- en Trading212-accounts komen in het gezamenlijke beeld
en in de gedeelde Wealthfolio-container terecht.

## Acceptance criteria

- [ ] Er is een tenant-scoped huishoudmodel met leden, uitnodigingen en
  statussen. Alleen een admin/eigenaar kan leden uitnodigen, rollen wijzigen of
  verwijderen; uitnodigingen zijn eenmalig, verlopen en lekken geen informatie
  over bestaande gebruikers.
- [ ] Iedere financiële account heeft een eigenaar en een expliciet
  zichtbaarheidbeleid (`private` of `household`). Bestaande accounts migreren
  veilig naar één gedocumenteerde standaard zonder onbedoeld breder zichtbaar
  te worden.
- [ ] Alle read-API's en afgeleide services (transacties, holdings, portfolio,
  performance, allocation, cashflow, net worth, dividenden en AI-samenvattingen)
  handhaven hetzelfde zichtbaarheidsbeleid en kunnen geen privédata via totals,
  filters, exports, webhooks, MCP of foutmeldingen lekken.
- [ ] Een gebruiker kan alleen eigen accounts delen of weer privé maken. De UI
  toont vóór bevestiging welke transacties, holdings en aggregaties daardoor in
  het huishoudbeeld verschijnen of verdwijnen.
- [ ] De Wealthfolio-exporter exporteert naar de gedeelde self-hosted instance
  uitsluitend accounts met zichtbaarheid `household`; privéaccounts worden
  nooit in die instance aangemaakt of bijgewerkt.
- [ ] Het huishoudbeeld aggregeert gedeelde bunq- en Trading212-accounts zonder
  dubbeltelling en behoudt per account de eigenaar/provenance voor uitleg en
  reconciliatie.
- [ ] Delen, intrekken, uitnodigen, accepteren, rolwijzigingen en verwijdering
  worden vastgelegd in een tenant-scoped security-auditlog zonder financiële
  payloads of secrets.
- [ ] Intrekken van delen stopt nieuwe export onmiddellijk. Reeds naar
  Wealthfolio geëxporteerde privé gemaakte data wordt volgens een expliciete,
  door de gebruiker bevestigde cleanup-flow verwijderd of in quarantaine gezet;
  er vindt geen stille destructieve verwijdering plaats.
- [ ] Tests met minimaal twee gebruikers bewijzen private-by-default,
  selectief delen, intrekken, RBAC, tenant-isolatie en het ontbreken van
  side-channel-lekken in aggregaties, exports, webhooks en MCP.
- [ ] OpenAPI-, control-panel- en beheerdocumentatie beschrijven uitnodigen,
  eigenaarschap, delen/intrekken, het gedeelde Wealthfolio-doel en herstel van
  foutieve deelinstellingen.

