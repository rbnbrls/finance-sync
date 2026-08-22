# Provider contract refresh

The compatibility source of truth is
`config/provider-contract-matrix.json`. It records the connector version,
capability set and fixture date. Account and transaction contracts are checked
for every listed connector; holdings are checked for Trading212 and DEGIRO
Pension. Security and FX contracts are added when a connector declares those
capabilities.

Refresh procedure:

1. Create a synthetic fixture from the provider's documented schema; never
   record credentials, names, account numbers or real financial values.
2. Update the fixture date and version in the matrix.
3. Run `uv run python scripts/provider_contract_refresh.py` and the provider
   contract tests.
4. Review failures for missing fields, type changes or enum changes before
   merging. Keep the previous fixture available for rollback comparison.

CI publishes the resulting contract report as a release artifact. Live
provider checks remain opt-in and secret-gated.
