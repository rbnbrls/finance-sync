# Managing Connector Connections

finance-sync lets a tenant hold **multiple connections for the same
provider**.  You can, for example, sync two different bunq logins (or one
bunq login and one Trading212 account) side by side in the same tenant —
each with its own credentials, label, paused/active state and account
selection.

This page is the operator/user guide for the connection model.  The
OpenAPI document (served live at `/openapi.json`) is the authoritative
source for request and response shapes.

## Provider health

The connection view exposes three independent levels: connection,
per-resource source data, and last successful processing. Show reauth-required
and rate-limit retry states with their safe reason codes and next action.
`connected` means only that credentials passed a provider check; it does not
mean that source data or the last sync is healthy.
API reference; the control panel at `/dashboard` exposes the same
operations in the UI.

---

## 1. The connection model

A **connection** is one stored credential set for one provider:

- **`connection_id`** — the stable identifier of the connection.  Every
  account, sync cursor, sync run and audit entry created by a sync is
  scoped to this id, so identical external account or transaction ids
  from two different connections never collide.
- **`provider_type`** — the connector key (`bunq`, `trading212`, …).
- **`description` / label** — a human-readable label you give the
  connection.
- **`status`** — `active` or `paused`.
- **`selected_accounts`** — the provider account ids this connection
  syncs; `null`/empty means *all* offered accounts.
- **`last_attempt_at` / `last_success_at` / `last_error`** — the sync
  outcome of this connection, with the error stored in sanitised form.

There is **no uniqueness constraint on (tenant, provider)** anymore: any
number of connections per provider per tenant is allowed.

### REST API

| Operation | Endpoint |
|---|---|
| List available connector types | `GET /api/v1/connectors` |
| List this tenant's connections | `GET /api/v1/connectors/configs` |
| Get one connection | `GET /api/v1/connectors/configs/{connection_id}` |
| Create a connection | `POST /api/v1/connectors/configs` |
| Update a connection | `PUT /api/v1/connectors/configs/{connection_id}` |
| Delete a connection | `DELETE /api/v1/connectors/configs/{connection_id}` |
| Test a connection | `POST /api/v1/connectors/configs/{connection_id}/test` |

Credentials are **never** returned by the API: create/update accept them,
the responses only carry the public fields listed above.  They are stored
AES-256-GCM encrypted in the `credentials` table and are decrypted only
inside the sync/test flow.

---

## 2. Account selection

After a successful connection test the provider's accounts are offered for
selection (the `accounts` array of the test response).  Only the selected
accounts are synced and exported downstream (e.g. to Wealthfolio).

```
POST /api/v1/connectors/configs/{connection_id}/accounts
{"account_ids": ["acc-123", "acc-456"]}
```

- An **empty list** resets the selection back to *all accounts*.
- Changing the selection **never deletes already-imported history**.  If
  you really want to remove locally stored data for accounts you
  deselected, pass `"purge_unselected": true` — this deletes the
  no-longer-selected accounts together with their transactions.  The
  purge is recorded in the audit log.

---

## 3. Pause / resume

Pausing a connection stops its **automatic** syncs; the data already
imported is kept untouched.

```
POST /api/v1/connectors/configs/{connection_id}/pause     # → status: paused
POST /api/v1/connectors/configs/{connection_id}/resume    # → status: active
```

A paused connection is skipped by the scheduler (see below) but can still
be synced manually — pausing is a "don't bother me" flag, not a lock.

---

## 4. Manual sync

Three trigger levels exist:

| Trigger | Behaviour |
|---|---|
| `POST /api/v1/sync` | Syncs all configured providers (every active connection of each). |
| `POST /api/v1/sync/{provider}` | Syncs one provider — every active connection of it; paused ones are skipped. |
| `POST /api/v1/sync/connections/{connection_id}` | Syncs exactly **one** connection, even when paused.  Returns 404 for unknown or foreign connection ids. |

A manual sync updates that connection's `last_attempt_at`,
`last_success_at` and sanitised `last_error` fields, and writes a
connection-scoped `SyncRun` (visible via `GET /api/v1/sync-runs`).

---

## 5. Scheduler behaviour

The scheduler (worker `sync_connector_job` / `sync_bunq_cards_job`)
iterates **every connection independently** on its tick:

- Each connection is processed in its own scoped unit; accounts, cursors
  and sync runs stay attached to the connection that produced them.
- A **failing connection never blocks its siblings**: the failing
  connection records a sanitised `last_error`, the others continue and
  complete normally.
- **Paused connections are skipped** (no `SyncRun` is created for them).
- Identical external ids from two different connections coexist without
  collisions thanks to connection-scoped unique constraints.

---

## 6. Error recovery

- Every connection exposes its last outcome (`last_attempt_at`,
  `last_success_at`, `last_error`); the error string is sanitised before
  it is stored or returned — credential values, bearer tokens and
  key=value secrets are redacted.
- Transient provider failures surface as `last_error` on the connection
  and the next scheduler tick (or a manual sync) retries automatically.
- To re-test a connection at any time, use the per-connection test
  endpoint — it updates the outcome fields and writes an audit entry.
- Deleting a connection stops future syncs for it but **keeps** the
  already-imported accounts, transactions and holdings (the connection id
  is retained on those rows for traceability).

---

## 7. Security

- Provider credentials are encrypted with AES-256-GCM (envelope
  encryption) at rest and are never included in API responses, audit
  entries, error messages, logs or metrics.
- Every lifecycle action — create, update, test, pause, resume, account
  selection, delete — writes a **tenant-scoped audit entry** (table
  `connection_audit_log`).  The audit trail is read-only for admins:
  `GET /api/v1/connectors/audit-log` (admin role required; filterable by
  `connection_id`, `provider_key`, `limit`).  Audit entries never contain
  credentials or financial payloads.
- All connector endpoints and the sync triggers require a valid bearer
  token (or API key) with the `connectors` / `sync` permission; every
  by-id operation is tenant-scoped — a foreign `connection_id` yields
  404, never data.

---

## 8. Upgrading from single-connection databases

Migration `0017_multi_connection` upgrades an existing single-connection
database backward-compatibly:

- the unique index on `(tenant_id, provider_key)` is dropped;
- existing configs are preserved byte-for-byte (ciphertext untouched) and
  get a generated label plus backfilled `connection_id`s on their
  accounts / transactions / cursors;
- new columns (`status`, `selected_accounts`, `last_attempt_at`,
  `last_success_at`, `last_error`) default to sensible legacy values.

See `docs/MIGRATIONS.md` and `docs/UPGRADE.md` for migration policy.
