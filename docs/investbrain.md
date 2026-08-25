# InvestBrain-koppeling

Finance-sync kan beleggingsrekeningen als InvestBrain-portfolios publiceren en
`purchase`/`sale` transacties via de officiële Sanctum REST API upserten.

## Configuratie

Gebruik een extern beheerde InvestBrain-installatie. Maak onder
**User → API Tokens** een token en zet dat token in `.env`:

```dotenv
INVESTBRAIN_SERVER_URL=http://localhost:18001
INVESTBRAIN_ACCESS_TOKEN=...
EXPORTER_INVESTBRAIN_ENABLED=true
```

Draai de koppeling met:

```bash
finance-sync investbrain push --days-back 3650
```

Controleer alleen de verbinding met `finance-sync investbrain push --dry-run`.

## Importcontract onderzocht

De `.xlsx` backup-import van InvestBrain gebruikt vier sheets:

| Sheet | Benodigde velden | Rol |
| --- | --- | --- |
| `Portfolios` | `title`; optioneel `portfolio_id`, `notes`, `wishlist` | Portfolios |
| `Transactions` | `symbol`, `portfolio_id`, `transaction_type`, `quantity`, `currency`, `date`; optioneel `transaction_id`, `cost_basis`, `sale_price`, `split`, `reinvested_dividend` | Trades |
| `Daily Changes` | `portfolio_id`, `date`; optioneel `annotation` | Historische grafieknotities |
| `Config` | `key`, `value` | Naam, locale, display currency en dividend-herinvestering |

`BUY` gebruikt `cost_basis` als prijs per unit; `SELL` gebruikt `sale_price`
als prijs per unit. Finance-sync stuurt alleen beleggingstransacties met een
security/ticker of ISIN; bankbetalingen, fees en dividenden worden niet als
trade verzonnen en worden als unsupported geteld.

Elk portfolio krijgt `finance-sync-account:<account-id>` in `notes`, zodat
volgende runs hetzelfde portfolio hergebruiken. Omdat de huidige create-API
geen extern ID accepteert, voorkomt de client duplicaten met een stabiele
business fingerprint van portfolio, symbool, type, hoeveelheid, prijs en datum.
