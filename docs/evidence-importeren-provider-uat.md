# Evidence — geïntegreerde importpagina provider-UAT

Datum: 2026-09-01  
Scope: DEGIRO Pensioen en SaxoInvestor bestandswizards, plus de gedeelde
Importeren-flow.

## Geautomatiseerde verificatie

- De aangeleverde Saxo-bestanden
  `Posities_28-aug-2026_17_33_40.xlsx` en
  `Transactions_15996986_2022-07-13_2026-08-28.xlsx` zijn als één paar
  gevalideerd: 9 posities en 251 transacties.

- `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -q`: **3739 passed, 208 skipped**.
- `uv run pytest -q tests/test_gui_dashboard.py`: **75 passed**.
- Ruff op gewijzigde Pythonbestanden: **groen**.
- `git diff --check`: **groen**.
- De regressiesuite controleert expliciet dat het kiezen van het laatste
  DEGIRO- of Saxo-bestand niet automatisch uploadt; de gebruiker start de
  controle expliciet met `Controle starten`.
- De UI valideert toegestane extensies, toont inline foutmeldingen en biedt
  retry voor upload en DEGIRO-confirm.

## Lokale runtime

De stack is opnieuw opgebouwd en gestart met:

```text
docker compose -f docker-compose.yml \
  -f docker-compose.local-bunq-e2e.yml \
  -f docker-compose.local-wealthfolio.yml up -d --build --wait
```

Daarna waren `app`, `worker`, `postgres`, `redis`, `bunq-mock` en
`wealthfolio` healthy; de eenmalige `migrate`-container eindigde met exit code
0. `GET /health/live` gaf `{"status":"ok"}`. De root/dashboard HTML bevat de
`Importeren`-pagina en de providerwizard-contracten.

## Interactieve browser-UAT

Uitgevoerd op 2026-09-01 in Safari tegen `localhost:8000` met de lokale
admin-gebruiker. Er zijn geen credentials of persoonlijke gegevens in dit
artifact opgenomen.

| Scenario | Resultaat | Waarneming |
|---|---|---|
| Eén Importeren-pagina en providerkeuze | Geslaagd | Bunq, Trading212, DEGIRO, Saxo, CSV en expenses worden vanuit dezelfde wizard gekozen. |
| DEGIRO drie rapportrollen | Geslaagd | Accountoverzicht, Transacties en Portefeuille; preview toont 7 transacties, 4 holdings en 11 regels; confirm voltooit met 12 nieuwe items. |
| SaxoInvestor twee rapportrollen | Geslaagd | Posities en Transacties; voltooide runs zichtbaar met 261/262 ingelezen regels. |
| CSV mapping en import | Geslaagd | Geldige CSV wordt gemapt en voltooid; ongeldige CSV toont ontbrekende kolommen inline. |
| Manual Expenses JSON | Geslaagd | Profiel aangemaakt, expenses-JSON gevalideerd en voltooid. |
| API profiel/verbinding/accountbeheer | Geslaagd | Bestaande Bunq-profielen tonen Verbinding testen en Accounts beheren; testactie voltooit zonder secrets terug te tonen. |
| Foutstatus en retry | Geslaagd | Ongeldige Saxo-run blijft als Mislukt in de historie; Opnieuw opent dezelfde Saxo-wizard met opnieuw te kiezen bestanden. |
| Toegankelijkheid van de flow | Geslaagd | Safari accessibility tree toont gelabelde knoppen, file-inputs, live-resultaatteksten en focusbare profielkeuze. |

De lokale browserchecks zijn aanvullend op de tenant-/permission-/idempotentie-
en responsive regressietests. De mobiele layout en screenreaderlabels zijn
daarmee contractueel/static getest; een afzonderlijke mobiele device-emulatie
was niet beschikbaar in deze Safari-sessie.

## Compatibiliteitslaag

De oude globale DEGIRO-functies blijven aanwezig voor mogelijke embedded
clients. De oude DOM-tree en automatische initialisatie zijn niet actief in de
dashboardpagina; de centrale flow gebruikt `/connectors/file-uploads/dispatch`.
