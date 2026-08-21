---
title: "Publiceer een controleerbare OpenAPI-contractdiff"
status: todo
priority: 20
---

## Context

Read-cleanups mogen geen API-contractwijziging veroorzaken. De release heeft
een reproduceerbare diff nodig in plaats van alleen een succesvolle generatie.

## Dependencies

Release 13 read-cleanup stories.

## Acceptance criteria

- [ ] Genereer OpenAPI voor de vorige en huidige release.
- [ ] Vergelijk paths, methods, parameters, response-schema's en security-
  schemes met een machineleesbaar diff.
- [ ] Additieve wijzigingen zijn expliciet toegestaan; breaking changes falen.
- [ ] Upload diff en beide snapshots als CI-artifacts.
- [ ] Voeg een regressietest toe voor de diff-policy.
