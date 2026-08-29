---
title: "Monitor encryptiesleutelversies en rotatieverval"
status: done
priority: 25
---

## Context

Managed key storage en rotatie zijn ingericht, maar verouderde, ontbrekende of
te binnenkort vervallende keyversies moeten actief worden gesignaleerd.

## Dependencies

Release 18 managed key provider.

## Acceptance criteria

- [x] Rapporteer actieve keyversie, rotatiedatum en expirystatus zonder
  keymateriaal.
- [x] Alert vóór expiry en bij onverwachte keyversion-downgrade.
- [x] Test provider outage, revoked key en gecontroleerde overgang.
- [x] Laat een onveilige keystatus staging/releasepromotie blokkeren.
- [x] Documenteer eigenaar en herstelprocedure.

## Implementatie en verificatie

- `src/finance_sync/services/key_status.py` rapporteert actieve keyversie,
  rotatiedatum, expirystatus en uren tot expiry zonder keymateriaal te
  ontsluiten (`material_exposed` is per ontwerp altijd `False`).
- `scripts/key_rotation_monitoring.py` alerteert vóór expiry
  (`KEY_ROTATION_ALERT_BEFORE_EXPIRY_HOURS`, default 24u), bij
  `key_version_downgrade` (kritiek) en bij `key_provider_error`, en maakt
  daarvoor gededupliceerde GitHub-issues aan.
- `scripts/check_key_status_for_promotion.py` + de `key-status-check`-job in
  `.github/workflows/release.yml` blokkeren staging/releasepromotie zolang de
  keystatus onveilig is (provider-error, expiry < 1u, material_logged).
- Tests: `tests/test_key_status.py`,
  `tests/test_key_rotation_monitoring.py`,
  `tests/test_check_key_status_for_promotion.py` en
  `tests/test_release18_managed_key_provider.py`.
- Verificatie: volledige suite groen, Ruff en Pyright geslaagd.

## Owner en herstelprocedure

- **Eigenaar**: finance-platform-oncall (zie `docs/MANAGED_KEY_PROVIDER.md`
  en `docs/AUTOMATED_DR_RUNBOOK.md`).
- **On-call contact**: finance-platform-oncall; incidenten worden
  gecreëerd als GitHub-issues `[Key Rotation] Alert: ...` door
  `scripts/key_rotation_monitoring.py`. Behandel het issue binnen het
  SLA-venster en escaleer naar het platformteam bij onduidelijkheid.
- **Basisrecovery**: herstel eerst provider-toegang en versiemetadata volgens
  de `recovery`-richtlijn in `config/managed-key-provider.json`, voordat
  encrypted backups worden ontsloten. Draai daarna rotatie volgens
  `docs/KEY_ROTATION.md`.

Herstel per onveilige keystatus:

- **Expired / expiring soon** (`key_approaching_expiry`):
  - Boven de 1u-grens: geplande rotatie uitvoeren volgens
    `docs/KEY_ROTATION.md` (nieuwe versie publiceren, re-encrypten,
    verifiëren, vorige versie na window retired).
  - Onder de 1u-grens of al verlopen: promotie is geblokkeerd; voer rotatie
    direct uit en herstart de release pas als `hours_to_expiry > 1`.
- **Revoked key**: provider retourneert een ingetrokken versie en faalt
  closed. Herstel de versiemetadata (welke versie is actief, welke retired),
  publiceer een nieuwe geldige versie, re-encrypt de envelopes en retire de
  ingetrokken versie.
- **Provider outage** (`key_provider_error`): herstel provider-toegang
  (credentials, netwerk, KMS/Vault-adapter) en controleer
  `fail_closed`-gedrag. De monitor blijft fail-closed en blokkeert promoties
  tot de provider weer status rapporteert.
- **Downgrade** (`key_version_downgrade`, kritiek): een onverwachte
  versiedaling duidt op een gereplayde oude provider-response of een
  verkeerde deploy. Herstel de actieve keyversie naar de laatst bekende
  geldige versie en onderzoek de oorzaak voordat verdere rotatie plaatsvindt.
- **Promotieblokkade**: zolang de keystatus onveilig is, faalt de
  `key-status-check`-job in `.github/workflows/release.yml`. Herstel eerst de
  keystatus; draai de release pas opnieuw als de gate groen is.
