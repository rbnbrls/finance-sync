---
title: "Publiceer een controleerbare OpenAPI-contractdiff"
status: done
priority: 20
---

## Context

De release publiceert een reproduceerbare OpenAPI-diff naast de twee snapshots.

## Acceptance criteria

- [x] Vorige en huidige OpenAPI-contracten worden gegenereerd.
- [x] Paths, methods, parameters, responses en security-schemes worden vergeleken.
- [x] Breaking changes falen; additieve wijzigingen zijn expliciet toegestaan.
- [x] Diff en snapshots worden als CI-artifacts geüpload.
- [x] De diff-policy heeft een regressietest.

## Implementatie en verificatie

- `check_openapi_diff.py` schrijft `openapi-diff.json` met policy findings.
- CI uploadt `openapi-base.json`, `openapi-head.json` en de diff.
- Verificatie: OpenAPI contracttests, Ruff en Pyright geslaagd.
- Tests: `tests/test_release14_openapi_contract.py`.
- CI/artifact: artifact `openapi-docs`.
