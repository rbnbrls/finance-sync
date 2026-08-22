# Managed key provider

`ManagedKeyProvider` is the provider-neutral boundary for KMS/Vault-style key
storage. Deployments provide a callback that fetches a version's material;
the application keeps only the current version, state and rotation metadata.
`LocalTestKeyProvider` is the safe in-memory double for local and unit tests.

Bootstrap binds the deployment identity and current key version in the staging
secret store. Rotation publishes a new version, re-encrypts and verifies the
synthetic fixture, then retires the previous version after the transition
window. Missing, invalid or revoked keys fail closed. Recovery restores
provider access and version metadata before reading encrypted backups.

Audit entries contain `encryption_key.rotated`, `from_version` and
`to_version`; key bytes and plaintext are never logged. To switch providers,
pause writes, validate the new adapter with synthetic data, then resume.
