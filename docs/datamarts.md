# Governed datamarts

Datamarts make downstream access an explicit policy decision instead of an
implicit consequence of a broad API-key permission. They expose only a
versioned Personal Finance Core projection and never provider credentials or
raw source payloads.

## Policy model

1. A **datamart** declares its dataset (`portfolio`, `cash-ledger`, etc.), PFC
   schema version, allowed fields, and delivery method (`pull_api`, `webhook`,
   `event_feed`, or `export`).
2. A **consumer** is the identity of one downstream tool. It can be bound to
   exactly one tenant API key.
3. A **grant** joins one consumer to one datamart. It selects either explicit
   accounts or all household-visible accounts, and can further reduce the
   datamart's field list.

The effective policy is fail-closed: an unknown requested field is removed and
an explicit empty account list grants no accounts.

## Administration API

All configuration endpoints require an administrator session:

- `POST /api/v1/datamarts` and `GET /api/v1/datamarts`
- `POST /api/v1/datamarts/consumers`
- `POST /api/v1/datamarts/grants`

`GET /api/v1/datamarts/consumers/{consumer_id}/policy` is available to the
tenant administrator or the API key bound to that consumer. It returns only
the effective policy, never secrets or delivery credentials.

Before a delivery adapter emits any record it must call
`account_is_allowed()` with the record account and its household visibility,
then project only the fields in `effective_grant().fields`.

## Example

Create `wealthfolio-portfolio-v1` with dataset `portfolio`, schema
`pfc/1.0`, fields `account_id`, `instrument_id`, `quantity`, and
`market_value`; bind it to a `wealthfolio` consumer and grant it the selected
household accounts. A future Wealthfolio delivery adapter can then consume its
policy instead of receiving all tenant investment data by default.
