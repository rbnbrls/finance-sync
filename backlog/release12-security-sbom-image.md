---
title: "Sluit security-, SBOM- en image-gates voor Release 12"
status: done
priority: 30
---

## Context

Release 12 vereist reproduceerbaar bewijs voor dependency-, SBOM- en
containerveiligheid voordat de modularisering als production-ready geldt.

## Acceptance criteria

- [x] `pip-audit` draait tegen de release-lockfile en faalt bij ongeaccepteerde
  kwetsbaarheden.
- [x] CycloneDX-SBOM wordt gegenereerd en als CI-artifact opgeslagen.
- [x] Trivy scant de gebouwde image met `.trivyignore` als tijdelijke,
  gemotiveerde en verlopen controleerbare uitzondering.
- [x] Iedere ignore-entry bevat rationale en expiry; verlopen entries falen.
- [x] Secrets, financiële waarden en credentials staan niet in logs of
  artifacts.
- [x] Scanresultaten zijn aan commit en image-tag gekoppeld.

## Implementatie en verificatie

- CI exporteert met `uv export --locked --no-dev` een requirementsbestand uit
  `uv.lock`; `pip-audit` en CycloneDX draaien expliciet tegen dat bestand.
- `.trivyignore` wordt door `scripts/check_trivyignore.py` gecontroleerd op
  rationale, unieke entries, geldige expiry-datums en verlopen uitzonderingen.
- CI en de releaseworkflow schrijven Trivy JSON-resultaten plus provenance
  met commit SHA en image-tag naar artifacts. De scan blijft fail-closed op
  HIGH/CRITICAL.
- Artifacts bevatten uitsluitend lockfile-afgeleide dependencydata,
  SBOM/scanresultaten en provenance; credentials of financiële datasetdata
  worden niet opgenomen.

Verificatie:

```text
uv run pytest tests/test_release12_security_gates.py \
tests/test_trivyignore_policy.py tests/test_release_ci_gate_contract.py -q
9 passed

uv run pyright -p pyrightconfig.tests.json \
tests/test_release12_security_gates.py scripts/check_trivyignore.py
0 errors

uv run python scripts/check_trivyignore.py .trivyignore
OK
```
