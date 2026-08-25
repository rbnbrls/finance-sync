# Implementatieplan fase 5 — datakwaliteit

## Doel

Maak datakwaliteit zichtbaar en herstelbaar vanuit de control plane. De
eerste increment gebruikt de bestaande reconciliatie-runs en canonieke
transacties als bron. Daardoor ontstaat geen tweede waarheid en blijft een
persistente issue-tabel uitgesteld tot acknowledgement/snooze daadwerkelijk
duurzame lifecycle-state nodig maakt.

## Scope van deze increment

1. Een `DataQualityOverview`-contract met:
   - laatste reconciliatie-run en status;
   - finding-aantallen per categorie en severity;
   - duplicate-, missing- en mismatch-findings;
   - coverage per provider/resource;
   - provenance van een finding (provider, account, transaction IDs en
     externe record IDs);
   - impact (aantal betrokken resources) en één herstelactie per issue.
2. Een tenant-scoped service die de meest recente reconciliation-resultaten
   projecteert en canonieke transacties per provider/account telt.
3. `GET /api/v1/control-plane/data-quality`, beschermd door de bestaande
   `reconciliation:read`-permissie.
4. De datakwaliteitsprojectie is vanuit de control plane zelfstandig
   opvraagbaar; het samenvoegen van de volledige issue-feed in de bestaande
   overview volgt nadat de UI-consument op dit contract is aangesloten.
5. Unit- en API-contracttests voor tenantisolatie, lege data, severity,
   provenance, impact en gesaneerde output.
6. De CI-coverage gate gaat van 70% naar 75%; de nieuwe code krijgt expliciete
   tests en de bestaande testset blijft de regressie-gate.

## Uitvoeringsvolgorde

1. Schemas en stabiele issue/action-mapping.
2. Datakwaliteitsservice met tenant-scoped queries.
3. Control-plane endpoint en overview-integratie.
4. Tests, coverage en kwaliteitschecks.

## Acceptatiecriteria

- Een tenant kan alleen eigen reconciliation-runs, findings en transacties
  zien.
- Een lege tenant krijgt een geldig, voorspelbaar contract.
- Findings tonen categorie, severity, oorzaak, provenance, impact en precies
  één concrete vervolgstap.
- Secrets, stack traces en ruwe payloads worden niet teruggegeven.
- Reconciliation-fouten worden als operationele aandacht gemarkeerd zonder
  de foutmelding ongefilterd te exposen.
- `ruff`, Pyright, alle unit-tests en coverage >= 75% zijn groen.

## Bewust uitgesteld

Issue acknowledgement/snooze, historische correcties, opnieuw normaliseren
en de persistente `control_plane_issues`-tabel volgen in een volgende
increment zodra lifecycle-state nodig is.
