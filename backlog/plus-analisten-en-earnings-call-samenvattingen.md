---
title: "Voeg analistenconsensus en Hermes-samenvattingen van earnings calls toe"
status: todo
priority: 11
---

## Context

Wealthfolio Connect Plus noemt analistenramingen en earnings-call summaries.
finance-sync heeft alleen losse forward EPS/PE-velden en geen consensusreeks,
estimate revisions, transcriptmetadata of brongebonden samenvattingen.

De lokale Hermes-agent op Proxmox verzorgt de samenvatting. finance-sync haalt
en valideert de onderliggende gegevens, respecteert bronrechten en slaat
resultaten met citaties en model/runmetadata op.

## Acceptance criteria

- [ ] Analistenconsensus ondersteunt per security en rapportageperiode minimaal
  EPS/omzet low, mean, high, aantal analisten, currency, observed-at en source;
  revisies blijven als tijdreeks beschikbaar.
- [ ] De API berekent estimate-revisions en dispersion deterministisch en maakt
  duidelijk wanneer providers uiteenlopen of dekking onvoldoende is.
- [ ] Earnings-call records bevatten eventdatum, kwartaal, deelnemers,
  document-/audio-URL, bronrechten en beschikbaarheidsstatus. Alleen content die
  lokaal verwerkt mag worden wordt aan Hermes aangeboden.
- [ ] Hermes produceert een gestructureerde samenvatting met minimaal resultaten,
  management guidance, belangrijkste risico's, vraag-en-antwoordthema's en
  expliciete onzekerheden; iedere sectie verwijst naar bronpassages of
  tijdcodes wanneer beschikbaar.
- [ ] De prompt instrueert Hermes om teksten als onbetrouwbare data te behandelen;
  instructies uit artikelen/transcripten kunnen geen tools, secrets of
  systeemgedrag activeren.
- [ ] Samenvattingen bevatten de Hermes-versie/modelidentiteit, promptversie,
  input-hashes, bron-ID's, generated-at en een stale/superseded-status zodat ze
  reproduceerbaar en ongeldig te maken zijn.
- [ ] Bij ontbrekende of niet-toegestane transcriptcontent toont het systeem
  alleen beschikbare earningsfeiten en links; Hermes genereert geen fictieve
  call summary.
- [ ] REST- en MCP-endpoints en de Wealthfolio companion view tonen consensus,
  revisies en call summaries tenant-scoped met citations en freshness.
- [ ] Tests gebruiken lokaal toegestane fixtures en dekken consensusberekening,
  revisies, bronconflicten, prompt-injection, ontbrekende content, Hermes-timeout
  en het voorkomen van ongeciteerde claims.
- [ ] Documentatie beschrijft bronlicenties, lokale verwerking, retentie,
  verwijdering en bekende coverage-gaps.

