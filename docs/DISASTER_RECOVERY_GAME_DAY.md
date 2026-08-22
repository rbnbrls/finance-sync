# Disaster-recovery game day

The Release 17 game day uses only the fixture in
`config/dr-game-day.json`. It covers database loss (backup restore), Redis
loss (rebuild cache/locks) and worker outage (restart and outbox replay).

Run locally with:

```bash
uv run python scripts/dr_game_day.py \
  --config config/dr-game-day.json \
  --artifact dr-game-day.json
```

The report records RPO, RTO, replayed/lost outbox events, final sync status,
tenant isolation and idempotency. It contains no credentials or financial
values. Every improvement action has an owner and a `next-release` deadline.
Production recovery follows the immutable-image rollback and PostgreSQL
restore procedures in `docs/RELEASING.md`; this game day does not operate on
production resources.
