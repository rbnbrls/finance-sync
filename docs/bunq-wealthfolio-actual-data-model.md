# Bunq → Finance-Sync → Wealthfolio / Actual Budget

## Besluit

Finance-Sync gebruikt één provider-neutraal canoniek model. De bestaande
`accounts`- en `transactions`-tabellen bevatten al de volledige
gemeenschappelijke brondata voor:

- Wealthfolio Spending & Budgets (cash/credit-account, cash activities,
  merchantgegevens, status, valuta en review/provenance).
- Actual Budget bank accounts (accountnaam, datum, signed bedrag, payee,
  notes, cleared-status en idempotente import-id).

We voegen daarom geen Wealthfolio- of Actual-specifieke kolommen toe aan de
bron-tabellen. Zulke waarden zijn doelconfiguratie of afgeleide exportdata.
De Bunq-providergegevens blijven in `provider_metadata` en de versieerbare,
privacy-gefilterde selectie in `provider_metadata_contract`.

## Gecombineerde gegevensbehoefte

| Finance-Sync-veld | Herkomst Bunq | Wealthfolio | Actual Budget |
| --- | --- | --- | --- |
| `accounts.external_account_id` | `MonetaryAccountBank.id` / `MonetaryAccountSavings.id` | `providerAccountId` | mapping naar AB account |
| `accounts.name` | `description` / accountnaam | Account Name | account name |
| `accounts.account_type` | bank/savings type | Cash of Credit Card | checking/savings/cash |
| `accounts.currency_code` | account balance currency | vaste accountvaluta | destination-configuratie |
| `accounts.current_balance` | Bunq balance | cash balance | balance reconciliation |
| `accounts.provider_metadata` | IBAN, Bunq subtype/status | provenance | provenance |
| `transactions.external_transaction_id` | `Payment.id` | `sourceRecordId` + idempotency | `imported_id` |
| `transactions.occurred_at` | `created` | activity date | transaction date |
| `transactions.booked_at` | `updated` | settlement date | cleared/posted timing |
| `transactions.amount` + `currency_code` | `amount.value/currency` | cash activity amount | integer minor units aan exportzijde |
| `transactions.description` | payment description/note | comment/notes | payee fallback/notes |
| merchantvelden + MCC | `merchant`, `mcc` | spending metadata | payee/notes |
| counterpartyvelden | `counterparty_alias` | transfer context | payee/transfer context |
| `status` + authorization/settlement | Bunq statusvelden | POSTED/PENDING/VOID + review | cleared |
| refund/fee/taxvelden | Bunq payment details | spending totalen | notes/category handling |
| `source_record_hash` + revision metadata | volledige Bunq response-hash | reproduceerbare update | veilige re-import |

### Doel-specifieke afleiding

Wealthfolio account-instellingen (`CASH`/`SECURITIES`, Cash Classification,
Tracking Mode en Active/Archived) horen bij de Wealthfolio-accountmapping.
Actual `offbudget`, `closed`, account groups en categorieën horen bij de
Actual-exportmapping. Ze zijn geen feiten die Bunq over een bankbetaling
levert en mogen dus niet de canonieke transactietabel vervuilen.

Voor Spending & Budgets worden `withdrawal`, `fee`, `interest` en vergelijkbare
uitgaven als spending verwerkt; refunds en credits verminderen spending.
Interne rekeningtransfers moeten aan beide kanten als transfer worden
herkend, zodat ze niet als uitgave dubbel tellen.

## Bunq API-ontwerp

1. Authenticeer één Bunq-installatie en hergebruik de installatie-, device- en
   session-state uit connector state. Bewaar private keys en tokens nooit in
   transaction metadata.
2. Lees accounts via
   `GET /v1/user/{user_id}/monetary-account?count=200`, met paginatie. Lees
   zowel `MonetaryAccountBank` als `MonetaryAccountSavings`.
3. Lees per monetary account betalingen via
   `GET /v1/user/{user_id}/monetary-account/{account_id}/payment?count=200`,
   eveneens met paginatie. Filter lokaal op `created >= sync_cursor`, omdat
   de providerresponse niet als enige incrementele garantie wordt gebruikt.
4. Sla account- en betalingsidentiteit, saldo, bedragen, valuta, timestamps,
   beschrijving, status, merchant, MCC, counterparty, refund, attachments en
   een hash van de bronresponse op volgens de tabel hierboven.
5. Upsert op `(tenant_id, provider_key, connection_id,
   external_transaction_id)`. Een gewijzigde Bunq-payment verhoogt de
   Finance-Sync revision en wordt opnieuw naar de destinations geëxporteerd.
6. Gebruik één exportcursor per destination. Wealthfolio krijgt
   cash-activities met stabiele source/idempotency-velden; Actual krijgt
   `importTransactions`-compatibele records met bedragen in integer minor
   units en een stabiele `imported_id`.

### Kaartbetalingen

`/card/{card_id}/card-payment` is aanvullende card-brondata. Deze wordt niet
automatisch naast dezelfde monetary-account payment als tweede banktransactie
geëxporteerd. Eerst wordt gecorreleerd op provider-id, hash, bedrag, valuta en
een tijdvenster. Alleen een niet-dubbele card payment wordt als afzonderlijke
card-transactie gebruikt; anders blijft de monetary-account payment de
canonical cashflow-record. Dit voorkomt dubbele spending in beide doelen.

## Referenties

- [Wealthfolio Spending & Budgets](https://wealthfolio.app/docs/guide/spending-budgets/)
- [Wealthfolio Accounts & Portfolios](https://wealthfolio.app/docs/guide/accounts/)
- [Wealthfolio Activity Fields](https://wealthfolio.app/docs/concepts/activity-fields/)
- [Actual Budget API reference](https://actualbudget.org/docs/api/reference/)
- [bunq Payment API](https://doc.bunq.com/payment/payment)
