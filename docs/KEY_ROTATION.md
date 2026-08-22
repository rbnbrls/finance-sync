# Encryption key rotation

The controlled states are `current`, `previous` and `retired`. During a
rotation, authorized services may read envelopes with `previous` and rewrite
them with `current`; after the migration window, `previous` is retired and
must fail decryption. Plaintext is never exported.

Run the synthetic drill with:

```bash
uv run python scripts/key_rotation_drill.py \
  --config config/key-rotation-drill.json \
  --artifact key-rotation-drill.json
```

The audit event is `encryption_key.rotated`. Rollback is allowed only before
retirement: restore the previous key state, stop new writes, and rerun the
read/re-encrypt job. After retirement, recover from the encrypted backup and
the documented key escrow; never print keys or plaintext in logs or tickets.
