# Data retention and privacy audit

The authoritative inventory is [`config/data-retention-policy.json`](../config/data-retention-policy.json).
It is validated in the CI security job and is reviewed every 90 days by the
privacy-operations owner.

The policy covers encrypted connector credentials, tenant-scoped audit data,
transactional outbox payloads, application/CI logs, canonical financial facts
and temporary provider payloads. Every category declares its storage location,
retention period, deletion/anonymisation action and rationale.

## Operator procedure

1. Work only on a tenant-specific staging copy or synthetic fixture.
2. Confirm the tenant scope and export an audit record before deletion.
3. Delete encrypted credentials with the connection; prune outbox and logs
   using their provider/database retention controls.
4. Delete or anonymise canonical financial facts according to the approved
   request and legal hold; never edit another tenant's rows.
5. Verify redacted logs and rerun `python scripts/check_data_retention_policy.py`
   plus the privacy tests. Do not paste financial exports, tokens or raw
   provider payloads into tickets.

Legal holds and subject requests override automated pruning and require an
operator audit trail. The policy does not authorize deletion of production
data by a developer or CI job.
