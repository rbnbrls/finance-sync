---
title: "Publiceer een controleerbare OpenAPI-contractdiff"
status: done
priority: 20
---

## Context

Read-cleanups mogen geen API-contractwijziging veroorzaken. De release heeft
een reproduceerbare diff nodig in plaats van alleen een succesvolle generatie.

## Dependencies

Release 13 read-cleanup stories.

## Acceptance criteria

- [x] Genereer OpenAPI voor de vorige en huidige release.
- [x] Vergelijk paths, methods, parameters, response-schema's en security-
  schemes met een machineleesbaar diff.
- [x] Additieve wijzigingen zijn expliciet toegestaan; breaking changes falen.
- [x] Upload diff en beide snapshots als CI-artifacts.
- [x] Voeg een regressietest toe voor de diff-policy.

## Implementatie en verificatie

- `check_openapi_diff.py` schrijft naast de console-uitvoer een JSON-report met
  breaking, allowlisted, additive en informational findings.
- CI uploadt base/head snapshots plus `openapi-diff.json`; breaking changes
  blijven een harde gate en additions zijn toegestaan.
- Verificatie: OpenAPI diff policy tests, API/OpenAPI-tests, Ruff, Pyright en
  `git diff --check` geslaagd.
- Tests: `tests/test_release14_openapi_contract.py`.
- CI/artifact: `openapi-base.json`, `openapi-head.json` en
  `openapi-diff.json` uit artifact `openapi-docs`.
