# Control-plane browser UAT evidence

Datum: 2026-09-01  
Omgeving: lokale Docker Compose-stack (`localhost:8000`, PostgreSQL, Redis)  
Browser: Safari  
Gebruiker: lokale admin-gebruiker (geen credentials opgenomen)

De interactieve herstelworkflow is keyboard-only doorlopen. De UI toonde
gelabelde acties, loading/error states en tenant-scoped statusinformatie; retry
keerde terug naar een terminale runstatus. De onderliggende permission-,
tenant-isolatie- en idempotentiegevallen zijn aanvullend door de regressie-
en contracttests afgedekt.

## Uitgevoerde scenario's

| Scenario | Resultaat | Evidence in UI |
|---|---|---|
| Control-plane overzicht openen | Geslaagd | Statusheader, actiecentrum, datakwaliteit, verbindingen en bestemmingen renderen. |
| Verbinding testen | Geslaagd | Lokale bunq fixture toont `Connection successful`. |
| Verbinding synchroniseren | Geslaagd | Lokale bunq fixture krijgt een nieuwe `last attempt`. |
| Sync-run detail/actiecentrum | Geslaagd | Mislukte runs tonen gesaneerde fouttekst, categorie en `Sync-details bekijken`. |
| Sync opnieuw proberen | Geslaagd | Retry gaat tijdens verwerking naar `Bezig…` en keert terug naar een terminale status. |
| Edit-flow | Geslaagd | Editformulier toont naam, masked credentialtekst en base URL; sluiten zonder wijziging werkt. |
| Pause/resume | Geslaagd | Gepauzeerde bunq-verbinding hervatten en daarna opnieuw pauzeren werkt. |
| Saxo test | Geslaagd | UI meldt dat SaxoInvestor is ingesteld en XLSX-import verwacht. |
| Trading212 foutpad | Geslaagd | UI toont `Trading212 request failed (HTTP 404)` zonder stack trace of secret. |
| Destination health checks | Geslaagd | Wealthfolio en Firefly tonen een recente health check met `ready`. |
| Wealthfolio export | Geslaagd | Laatste export wordt `Voltooid` met bijgewerkte timestamp. |
| Firefly export | Geslaagd | Export gaat via `Bezig…` naar `Voltooid` met bijgewerkte timestamp. |
| Data health | Geslaagd | Bronstatus, coverage, freshness en reconciliatiebevindingen zijn zichtbaar. |

## Afbakening

De UAT gebruikte uitsluitend bestaande lokale, tenant-scoped testdata. Er zijn
geen credentials, secrets of providerdata in dit artifact opgenomen. De
remote GitHub Actions-run kan niet worden bevestigd voor een nog niet
gepubliceerde werkboom; de lokaal CI-equivalente gates blijven de bron voor
deze validatie.
