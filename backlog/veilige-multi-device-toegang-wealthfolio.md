---
title: "Bied veilige multi-device toegang tot de self-hosted Wealthfolio-instance"
status: done
priority: 20
---

## Context

Wealthfolio Connect synchroniseert afzonderlijke lokale databases tussen
desktop, mobiel en self-hosted installaties met end-to-end-encryptie. Voor het
beoogde gratis self-hosted resultaat is een eenvoudiger en beter controleerbaar
equivalent beschikbaar: de Wealthfolio web/PWA-container op Proxmox is de enige
database en alle apparaten gebruiken diezelfde instance. Daardoor zijn er geen
cloudrelay en geen conflicterende databasekopieën nodig en blijft alle
portfoliodata op de eigen server.

finance-sync bevat nog geen reproduceerbare deployment en verificatie voor die
multi-device route. Dit verhaal levert veilige browser/PWA-toegang vanaf
desktop, iOS en Android, met transportbeveiliging, lokaal beheerde sleutels en
herstelbare backups. Het verhaal claimt nadrukkelijk geen offline sync tussen
afzonderlijke native Wealthfolio-databases; die beperking moet zichtbaar zijn
in de documentatie.

## Acceptance criteria

- [x] De Wealthfolio-container op Proxmox LXC 104 is vanaf minimaal een
  desktopbrowser en een mobiel/PWA-apparaat via één stabiele HTTPS-URL
  bereikbaar en beide tonen aantoonbaar dezelfde bunq- en Trading212-data.
- [x] De containerpoort is niet rechtstreeks vanaf internet bereikbaar. Een
  gedocumenteerde reverse-proxy-, VPN- of private-tunnelconfiguratie verzorgt
  TLS, veilige cookie-forwarding en correcte `WF_CORS_ALLOW_ORIGINS`-instelling.
- [x] Authenticatie gebruikt Wealthfolio's ondersteunde password- of
  OIDC-configuratie. Standaardcredentials zijn verwijderd, brute-force-risico
  is beperkt en sessies kunnen centraal worden ingetrokken.
- [x] `WF_SECRET_KEY`, Wealthfolio-authenticatiegegevens en finance-sync-secrets
  staan alleen in de secret store/environment van de deployment, nooit in git,
  images, client-side code, logs of documentatie.
- [x] De Wealthfolio-data, secrets en finance-sync-database worden uitsluitend
  op opslag onder beheer van de gebruiker bewaard. Backups zijn versleuteld met
  een lokaal beheerde sleutel en bevatten geen plaintext secrets buiten de
  Proxmox-omgeving.
- [x] Er is een automatische backup met retentie en een geteste restore naar
  een tijdelijke instance. De restoretest bewijst dat accounts, activiteiten,
  holdings en de finance-sync delivery-cursors behouden blijven.
- [x] Een netwerk-/privacytest bewijst dat finance-sync geen portfolio- of
  credentialpayloads naar Wealthfolio Connect, SnapTrade of andere niet
  geconfigureerde derden verstuurt. Benodigde marktdata- en providerrequests
  zijn gedocumenteerd.
- [x] De PWA is installeerbaar op ondersteunde mobiele browsers en een korte
  runbook beschrijft installatie, login, sleutelbackup, certificaatvernieuwing,
  apparaatverlies en herstel.
- [x] Monitoring controleert HTTPS-bereikbaarheid, certificaatverval,
  Wealthfolio-health en de versheid van bunq/Trading212-export zonder financiële
  waarden of secrets als metriclabels te publiceren.
- [x] Een end-to-end-test of geautomatiseerde smoke-run verifieert de route
  `provider -> finance-sync -> Wealthfolio -> twee clients` en faalt wanneer
  één client verouderde data ziet.
- [x] De documentatie legt expliciet het verschil uit met Wealthfolio Connect:
  online clients delen één self-hosted database; onafhankelijke native/offline
  databases worden niet door deze oplossing gerepliceerd.

