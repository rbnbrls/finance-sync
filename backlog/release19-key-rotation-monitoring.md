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

- [ ] Rapporteer actieve keyversie, rotatiedatum en expirystatus zonder
  keymateriaal.
- [ ] Alert vóór expiry en bij onverwachte keyversion-downgrade.
- [ ] Test provider outage, revoked key en gecontroleerde overgang.
- [ ] Laat een onveilige keystatus staging/releasepromotie blokkeren.
- [ ] Documenteer eigenaar en herstelprocedure.
