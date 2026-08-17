# Veilige multi-device toegang tot de self-hosted Wealthfolio-instance

Status: actief · Geldig voor de Wealthfolio deployment op Proxmox LXC 104
(192.168.3.50:8080) · Zie `backlog/veilige-multi-device-toegang-wealthfolio.md`
voor de story en acceptance criteria.

Deze pagina beschrijft hoe desktopbrowsers en mobiele PWA's veilig gebruikmaken
van **één gedeelde, self-hosted Wealthfolio-database** via één stabiele
HTTPS-URL, en hoe die toegang wordt beveiligd, geback-upt en bewaakt. Het
verschil met Wealthfolio Connect staat in [§Verschil met Wealthfolio
Connect](#verschil-met-wealthfolio-connect).

## 1. Architectuur: één database, alle apparaten

Alle clients (desktopbrowser, iOS/Android PWA) gebruiken dezelfde
Wealthfolio-server op LXC 104. De SQLite-database in
`/opt/wealthfolio_data/wealthfolio.db` is de enige bron van waarheid; er zijn
geen afzonderlijke lokale databasekopieën en geen cloudrelay.

```
┌──────────────┐   ┌──────────────┐
│ Desktop      │   │ Mobiele PWA  │   (iOS Safari / Android Chrome,
│ browser      │   │ (iOS/Android)│    installeerbaar via manifest.json)
└──────┬───────┘   └──────┬───────┘
       │                  │
       └────────┬─────────┘
                ▼
      https://wealthfolio.7rb.nl            ← één stabiele HTTPS-URL
                │
       Cloudflare edge (TLS, DNS proxy)
                │
       cloudflared tunnel "proxmox"         ← LXC 102, outbound-only
                │
       Coolify Traefik (LXC 100, wildcard   ← Let's Encrypt *.7rb.nl
       *.7rb.nl-certificaat)                   
                │
       wealthfolio-proxy (nginx, Coolify)   ← deploy/wealthfolio-proxy/
                │                                proxy_pass, SSE-safe
                ▼
       Wealthfolio-server :8080 (LXC 104)   ← nooit rechtstreeks op
                                                 internet gepubliceerd
```

De containerpoort (8080) is **niet rechtstreeks vanaf internet bereikbaar**:
het enige pad naar buiten loopt via de Cloudflare-tunnel (outbound
verbindingen vanaf LXC 102). De poort is alleen bereikbaar op het LAN
(192.168.3.50:8080) en via de proxyketen hierboven. Dit is de
"private-tunnelconfiguratie" uit de acceptance criteria.

## 2. TLS, cookies en CORS

- **TLS**: Cloudflare termineert TLS op de edge (Universal SSL, Let's Encrypt,
  automatische vernieuwing). Traefik presenteert het wildcard-certificaat
  `*.7rb.nl` aan de tunnel. Er is geen zelfbeheerd certificaat te vernieuwen;
  zie [§Runbook — certificaatvernieuwing](#certificaatvernieuwing).
- **Cookie-forwarding**: de nginx-proxy stuurt de `Host`-header en cookies
  ongewijzigd door (`proxy_set_header Host $host`), zodat Wealthfolio zijn
  sessiecookies op het publieke hostname `wealthfolio.7rb.nl` zet en de PWA
  same-origin blijft laden.
- **SSE**: de Wealthfolio UI gebruikt server-sent events; de proxy zet
  `proxy_buffering off` en ruime timeouts, zodat live-updates niet worden
  gebufferd (zie `deploy/wealthfolio-proxy/nginx.conf`).
- **CORS**: `WF_CORS_ALLOW_ORIGINS` in `/opt/wealthfolio/.env` moet exact het
  publieke origin bevatten:

  ```ini
  WF_CORS_ALLOW_ORIGINS=https://wealthfolio.7rb.nl,http://192.168.3.50:8080
  ```

  (komma-gescheiden lijst; `*` wordt door Wealthfolio geweigerd). De eerste
  entry is het origin dat browsers in de adresbalk zien; de tweede is voor
  LAN-testen. Na een wijziging: `systemctl restart wealthfolio`.

## 3. Authenticatie en sessies

- Wealthfolio draait met **password-authenticatie** (argon2id-hash in
  `WF_AUTH_PASSWORD_HASH`). OIDC is uitgeschakeld
  (`/api/v1/auth/status` → `{"requiresPassword":true,"oidcEnabled":false}`).
- **Geen standaardcredentials**: het wachtwoord is bij de eerste inrichting
  gegenereerd en staat alleen in `/root/wealthfolio.creds` op LXC 104 (en in
  de password-manager van de eigenaar) — nooit in git, images of
  documentatie.
- **Brute-force**: de Wealthfolio-server logt mislukte logins; de exposure is
  beperkt omdat de poort alleen via de tunnel bereikbaar is en Cloudflare
  rate-limiting/security-functies voor de publieke URL actief zijn. Bij
  vermoeden van brute-force: wachtwoord roteren (§Runbook).
- **Centrale sessie-intrekking**: een sessie is een door de server
  uitgereikte cookie; het wachtwoord wijzigen (`WF_AUTH_PASSWORD_HASH`
  vervangen + `systemctl restart wealthfolio`) maakt alle uitstaande
  sessiecookies ongeldig — dé manier om een verloren apparaat centraal uit
  te sluiten.

## 4. Secrets: waar ze wél en nooit mogen staan

| Geheim | Bewaarplaats | Nooit in |
|---|---|---|
| `WF_SECRET_KEY` (32-byte sessiesleutel) | `/opt/wealthfolio/.env` (chmod 600, root-only, LXC 104) | git, images, client-side code, logs, docs |
| `WF_AUTH_PASSWORD_HASH` | idem | idem |
| Wealthfolio-wachtwoord (`WF_PASSWORD`) | `/root/wealthfolio.creds` (LXC 104) + password-manager | idem |
| finance-sync secrets (`SECRET_KEY`, `MASTER_ENCRYPTION_KEY`, `POSTGRES_PASSWORD`, connector-credentials) | Coolify env-store (encrypted) / app-database | idem |
| Backup-sleutel | `/root/.wealthfolio-backup.key` op de Proxmox-host (chmod 600) | idem |

Deze inventaris wordt door `tests/test_wealthfolio_network_privacy.py` en de
monitor bewaakt: uitgaande verkeer gaat uitsluitend naar de geconfigureerde
server, en metric-labels bevatten nooit financiële waarden of secrets.

## 5. Backup en restore

Het backup-systeem staat in `deploy/wealthfolio/` en draait volledig op
opslag onder beheer van de gebruiker (Proxmox-host + LXC 104):

- **Snapshot** (LXC 104, `wealthfolio-snapshot.timer`, dagelijks 03:17 UTC):
  consistente SQLite-kopie via de online backup-API (WAL-veilig, server blijft
  draaien) → `/opt/wealthfolio_data/snapshot/wealthfolio-snapshot.db`.
- **Bundle** (Proxmox-host, `wealthfolio-backup.timer`, dagelijks 03:47 UTC):
  `backup.py backup` trekt de snapshot (`pct pull`), dumpt desgewenst de
  finance-sync delivery-cursors (`wealthfolio_deliveries`,
  `wealthfolio_account_mappings`, `export_runs` uit PostgreSQL op Coolify),
  bundelt beide in een tar.gz en **versleutelt** met AES-256-CBC (openssl,
  PBKDF2, 200k iteraties) onder `/root/.wealthfolio-backup.key`.
- **Retentie**: 14 dagelijks + 8 wekelijks + 6 maandelijks
  (bucket-union; overschrijdingen worden gepruneed).
- **Verificatie**: elke verse bundle wordt direct terug-gedecrypt en
  gecontroleerd (SQLite `PRAGMA integrity_check` + vereiste tabellen
  `accounts`/`activities`/`holdings_snapshots`/`assets`). De unit-tests in
  `tests/test_wealthfolio_backup_restore.py` bewijzen de round-trip.

**Restore naar een tijdelijke instance** (geteste procedure, AC):

```bash
# 1. Kies een bundle en decrypt + verifieer (op de Proxmox-host):
python3 /opt/wealthfolio-backup/backup.py restore \
  --backup-file /var/backups/wealthfolio/wealthfolio-backup-<stamp>.enc \
  --key-file /root/.wealthfolio-backup.key \
  --target-db /var/backups/wealthfolio/staging/restored.db

# 2. Start de tijdelijke instance (LXC 107 "wf-restore-tmp"):
pct start 107
pct push 107 /var/backups/wealthfolio/staging/restored.db /opt/wealthfolio_data/wealthfolio.db
pct exec 107 -- systemctl restart wealthfolio

# 3. Bewijs dat accounts, activiteiten, holdings behouden zijn:
curl -s http://<tmp-ip>:8080/api/v1/auth/status   # requiresPassword: true
curl -s -X POST http://<tmp-ip>:8080/api/v1/auth/login -d '{"password": ...}'
curl -s http://<tmp-ip>:8080/api/v1/accounts       # zelfde accounts als prod

# 4. Delivery-cursors: importeer de cursors-dump in de finance-sync DB
#    (of vergelijk wealthfolio_deliveries voor/na). De restore-test in
#    tests/test_wealthfolio_backup_restore.py dekt dezelfde round-trip.
```

De restoretest bewijst dat **accounts, activiteiten, holdings én de
finance-sync delivery-cursors** behouden blijven: de cursors zitten in
dezelfde versleutelde bundle.

## 6. Monitoring

`finance-sync-wealthfolio-monitor` (nieuw console script;
`src/finance_sync/monitoring/wealthfolio_monitor.py`) controleert:

| Check | Wat | Faalt wanneer |
|---|---|---|
| `https_reachable` | GET op de publieke URL | geen HTTP 200 met PWA-HTML |
| `cert_expiry` | TLS-certificaat van de publieke URL | < 21 dagen resterend |
| `wealthfolio_health` | `/api/v1/auth/status` | niet 200 óf `requiresPassword=false` |
| `export_freshness` | nieuwste `wealthfolio_deliveries.last_exported_at` | ouder dan `--max-stale-hours` (24) of geen data |

Prometheus-output (stdout) gebruikt alleen vaste `check`-labels — **geen
financiële waarden, account-ids of secrets in metriclabels**. Exitcode 1 bij
elke kritieke fout. Schema via systemd op de Proxmox-host of een willekeurige
scheduler:

```bash
finance-sync-wealthfolio-monitor \
  --public-url https://wealthfolio.7rb.nl \
  --database-url "$DATABASE_URL" \
  --max-stale-hours 24
```

## 7. PWA-installatie (runbook)

1. **Installeer**: open `https://wealthfolio.7rb.nl` in Safari (iOS: Delen →
   *Zet in beginscherm*) of Chrome (Android: menu → *App installeren*).
   Desktop: Chrome/Edge → *Installeren*. De PWA is herkenbaar aan
   `manifest.json` + service worker.
2. **Login**: voer het Wealthfolio-wachtwoord in (uit je password-manager;
   dit is géén finance-sync credential).
3. **Sleutelbackup**: er is geen aparte device-sleutel — alle data staat in
   de gedeelde serverdatabase. De enige "sleutel" is het wachtwoord en de
   backup-sleutel `/root/.wealthfolio-backup.key` (bewaar een kopie in je
   password-manager; zonder die sleutel zijn backups niet te ontsleutelen).
4. **Certificaatvernieuwing**: automatisch (Cloudflare Universal SSL /
   Let's Encrypt + Traefik wildcard). De monitor waarschuwt als er < 21
   dagen resteren. Handmatig ingrijpen is niet nodig.
5. **Apparaatverlies**: verander het Wealthfolio-wachtwoord (nieuwe
   `WF_AUTH_PASSWORD_HASH` + restart) — alle sessiecookies op alle apparaten
   zijn dan ongeldig. Het verloren apparaat kan daarna niet meer inloggen.
6. **Herstel**: bij dataverlies volg §5 (restore naar tijdelijke instance,
   dan terug naar productie).

## 8. Netwerk- en privacytest (AC)

`tests/test_wealthfolio_network_privacy.py` bewijst:

- elke HTTP-request van de Wealthfolio-client gaat naar de **geconfigureerde**
  `WEALTHFOLIO_SERVER_URL` en nergens anders (recording-transport);
- credentials worden alleen naar het login-endpoint van die server gestuurd;
- de exporter-/connector-broncode bevat **geen hardcoded externe hosts**
  (Wealthfolio Connect, SnapTrade of andere derden).

De enige benodigde externe requests van finance-sync zijn de geconfigureerde
provider-API's (bunq, Trading212) en de geconfigureerde Wealthfolio-server;
marktdataverrijking via OpenBB is optioneel en apart geconfigureerd.

## 9. End-to-end controle (AC)

`scripts/wealthfolio_multi_client_smoke.py` verifieert live de route
`provider -> finance-sync -> Wealthfolio -> twee clients`:

```bash
WF_PASSWORD='...' DATABASE_URL='postgresql://...' \
  python3 scripts/wealthfolio_multi_client_smoke.py \
  --public-url https://wealthfolio.7rb.nl --max-stale-hours 24
```

De run **faalt (exit 1)** wanneer één client verouderde data ziet: twee
onafhankelijke sessies moeten identieke accounts en activiteiten tonen, en de
delivery-cursor mag niet ouder zijn dan de staleness-grens. De geautomatiseerde
variant staat in `tests/test_wealthfolio_multi_client.py`.

## Verschil met Wealthfolio Connect

**Wealthfolio Connect** synchroniseert afzonderlijke lokale databases tussen
desktop, mobiel en self-hosted installaties met end-to-end-encryptie via een
cloudrelay. Deze oplossing doet dat **niet**:

- Online clients delen **één self-hosted database** (LXC 104); er wordt niets
  naar een relay of cloud gestuurd.
- **Onafhankelijke native/offline databases worden niet gerepliceerd**: er is
  geen offline-sync, geen conflictresolutie en geen Connect-relay. Een apparaat
  zonder netwerk toont geen portfoliodata.
- Voordeel: geen cloudrelay, geen conflicterende databasekopieën, alle data
  blijft op de eigen server, één simpel backup-/restore-model.

Kies Connect alleen als je échte offline databases op meerdere apparaten wilt
laten synchroniseren; kies deze opstelling als één gedeelde, online database
volstaat.

## Zie ook

- `deploy/wealthfolio/backup.py` + `restore.py` + systemd-units — backup/restore
- `deploy/wealthfolio-proxy/` — nginx-proxy voor de publieke HTTPS-URL
- `src/finance_sync/monitoring/wealthfolio_monitor.py` — monitoring
- `scripts/wealthfolio_multi_client_smoke.py` — two-client smoke run
- `tests/test_wealthfolio_{network_privacy,backup_restore,monitoring,multi_client}.py`
- `evidence-multi-device-access.md` — live-verificatie van deze story
