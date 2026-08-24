# Implementatieplan fase 4 — uniforme actie- en permissielaag

## Doel

Maak iedere actie die vanuit de control plane wordt aangeboden expliciet,
tenantveilig en uitvoerbaar volgens één contract. De overview geeft niet
alleen een label en URL terug, maar ook de stabiele actienaam, vereiste
permissie, destructiviteit en actuele beschikbaarheid voor de ingelogde
principal.

## Scope

1. **Actiecontract**
   - Breid `ControlPlaneAction` uit met `key`, `permission`, `destructive`,
     `enabled` en optioneel `disabled_reason`.
   - Gebruik een centrale catalogus met alleen toegestane action keys en hun
     canonieke HTTP-methode, resource-permissie en routepatroon.
   - Houd routes parameterized en tenant-neutraal; ids mogen alleen uit de
     tenant-scoped projection komen.

2. **Autorisatieprojectie**
   - Geef de control-plane service een permission resolver voor JWT-rollen en
     API-key permissions.
   - De service markeert acties waarvoor de principal geen permissie heeft als
     `enabled=false` met een veilige reden. De overview lekt geen objectdata
     buiten de tenant.
   - Gebruik de bestaande resource-permissies: `connectors`, `sync`,
     `securities`, `enrichment` en `destinations`. Destination-mutaties
     blijven admin-only zolang de bestaande API dat vereist.

3. **Actie mapping**
   - `test_connection`, `sync_connection`, `view_sync_run`, `retry_sync`,
     `map_security`, `view_data_source`, `test_destination`, `run_export` en
     `retry_export` krijgen vaste metadata.
   - Elke actionable issue bevat precies één actie; herstelacties zijn POST en
     worden als `destructive` gemarkeerd wanneer ze externe state kunnen
     wijzigen. Read-only acties blijven GET.

4. **Veiligheid en concurrency**
   - Voeg tests toe voor readonly users, ontbrekende permissies, API keys,
     cross-tenant ids, gepauzeerde connections, verlopen destinations,
     dubbele clicks en gelijktijdige retries.
   - Maak retry- en run-acties idempotent op requestniveau: een tweede poging
     terwijl dezelfde connection/destination al actief is geeft een stabiele
     conflictrespons en start geen tweede run.
   - Behoud bestaande endpoint-authenticatie en voeg ontbrekende guards toe
     voordat een actie in de UI enabled kan worden.

## Uitvoeringsvolgorde

1. Contract, action catalogus en permission resolver.
2. Service- en API-integratie met auth-context.
3. Guards en concurrency/idempotency op sync-, security- en destination-
   actieroutes.
4. Unit-, API- en GUI-contracttests voor positieve en negatieve paden.
5. Ruff, Pyright, volledige unit-testset met coverage en beschikbare
   integratiechecks.

## Acceptatiecriteria

- Iedere actie valideert tegen de catalogus en bevat alle verplichte velden.
- Een readonly gebruiker ziet read-acties enabled en write-acties disabled.
- Een ontbrekende of ongeldige permissie kan geen mutatie uitvoeren.
- Een id uit een andere tenant geeft geen data en start geen actie.
- Gepauzeerde connections en niet-actieve/verlopen destinations bieden geen
  uitvoeractie zonder duidelijke `disabled_reason`.
- Dubbele clicks en gelijktijdige retries maken maximaal één operationele run.
- Geen secrets, stack traces of cross-tenant metadata verschijnen in responses.
- `ruff check`, formatter, Pyright en de CI-unitjob zijn groen.

## Buiten scope

Persistente issues, acknowledge/snooze, nieuwe connectors, multi-user-
functionaliteit en inhoudelijke datakwaliteitsanalyse blijven fase 5 of later.
