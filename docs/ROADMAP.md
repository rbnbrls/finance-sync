# Roadmap

This page describes active product directions. Completed work belongs in the
code and the capability map in [roadmap-coverage.md](roadmap-coverage.md), not
in a numbered release-plan archive.

## Current priorities

1. Keep the canonical API and generated OpenAPI contract stable.
2. Strengthen PostgreSQL/Redis integration, migration and E2E coverage.
3. Improve connector certification, retry behavior and import diagnostics.
4. Extend destination observability, delivery recovery and privacy-safe
   operational evidence.
5. Keep market-intelligence and holding-relevance providers modular and
   credential-safe.

## Delivery rules

- New schema changes use Alembic and expand/contract sequencing.
- New connectors expose capabilities, have fixtures and do not leak provider
  SDK models into the core.
- New exporters are isolated adapters with replay-safe delivery and failure
  visibility.
- Every feature has unit tests; infrastructure-dependent behavior also gets
  integration or E2E coverage where appropriate.
- Secrets and real financial payloads stay out of tests, logs and artifacts.

## Planning guidance

Use GitHub issues and the codebase as the task tracker. Do not create another
release-number document for an implementation plan. When a roadmap item is
started, link the issue or pull request from the relevant feature document and
update [roadmap-coverage.md](roadmap-coverage.md) only after the code and
verification are complete.
