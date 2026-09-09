# GUI-routes en endpoints

Dit document beschrijft alle gebruikersroutes in het dashboard en de API-calls die door de GUI worden gebruikt. De API-prefix is `/api/v1`; de dashboardcode voegt die prefix zelf toe. Een gebruiker hoort API-URL's niet rechtstreeks te openen: acties die uit een issue komen blijven binnen de geauthenticeerde GUI.

## Dashboardroutes

| GUI-route | Sectie | Functie |
| --- | --- | --- |
| `/` of `/#overview` | Overzicht | Status van de volledige dataketen, actiecentrum, bronnen, syncs en bestemmingen |
| `/#data-health` | Data health | Datakwaliteit, freshness, bronnen en herstelacties |
| `/#connectors` | Verbindingen | Connectorcatalogus, verbindingen, credentials, accounts, testen, pauzeren en synchroniseren |
| `/#viewer` | Viewer | Read-only accounts, holdings en recente transacties |
| `/#uploads` | Importeren | Provider kiezen, API-/bestandsimport, DEGIRO/Saxo-flow en uploadhistorie |
| `/#exporters` | Exporters | Bestemmingen beheren, testen, previewen, exporteren en sleutels roteren |
| `/#sync` | Sync Runs | Schedules, filters, runhistorie, details en retry/reset-acties |
| `/#holding-news` | Holdingnieuws | Feed, filters, kalender en gelezen/ongelezen markering |
| `/#settings` | Settings | Thema wisselen |
| `/login` | Login | Inloggen; na login terug naar de oorspronkelijke route via `next` |

## API-calls per GUI-sectie

Alle calls lopen via de dashboard-helper `api()` met de bearer-token, behalve multipart-uploads en notebook-downloads; die voegen dezelfde `authHeaders()` expliciet toe.

### Globaal en Overzicht

| Methode | Endpoint | Gebruikersactie |
| --- | --- | --- |
| GET | `/auth/me` | Profiel en permissies laden |
| GET | `/control-plane/overview` | Overzicht laden/vernieuwen |
| GET | `/control-plane/data-quality` | Datakwaliteit in Overzicht |
| POST | `/feedback` | Feedbackformulier verzenden |

### Data health en issue-herstel

| Methode | Endpoint | GUI-bestemming |
| --- | --- | --- |
| GET | `/control-plane/data-health` | Data Health laden/vernieuwen |
| GET | `/connectors/configs` | Connectorprobleem: geauthenticeerde bewerk/reauth-modal |
| GET | `/enrichment/status` | Koersstatus inline tonen; daarna Data Health opnieuw controleren |
| GET | `/accounts?limit=200` | Accountconflict naar Viewer |
| GET | `/transactions?limit=50&sort_order=desc` | Transactieprobleem naar Viewer |
| GET | `/connectors/file-uploads/runs` | Importprobleem naar Importeren |
| GET | `/sync-runs/{run_id}` | Sync-detail binnen Sync Runs |
| POST | `/sync-runs/{run_id}/retry` | Mislukte sync opnieuw proberen |
| GET | `/reconciliation/{run_id}` | Finding-detail binnen Data Health |
| GET | `/securities/unresolved` | Security-mappingflow binnen Verbindingen |

API-actie-URL's uit Data Health worden niet met `window.location` geopend. Onbekende API-actie-URL's worden geweigerd met een GUI-melding.

### Verbindingen en connectors

| Methode | Endpoint |
| --- | --- |
| GET | `/connectors` |
| GET | `/connectors/configs` |
| GET | `/connectors/{connection_id}/health` |
| GET | `/connectors/{connection_id}/health?refresh=true` |
| POST | `/connectors/{provider}/test` |
| POST | `/connectors/configs` |
| PUT | `/connectors/configs/{connection_id}` |
| DELETE | `/connectors/configs/{connection_id}` |
| POST | `/connectors/configs/{connection_id}/test` |
| POST | `/connectors/configs/{connection_id}/accounts` |
| POST | `/connectors/configs/{connection_id}/pause` |
| POST | `/connectors/configs/{connection_id}/resume` |
| GET | `/connectors/degiro-pension/imports` |
| POST | `/connectors/degiro-pension/imports/preview` |
| POST | `/connectors/degiro-pension/imports/{run_id}/confirm` |

### Viewer

| Methode | Endpoint |
| --- | --- |
| GET | `/accounts?limit=200` |
| GET | `/portfolio` |
| GET | `/holdings?limit=500` |
| GET | `/transactions?limit=50&sort_order=desc` |

### Importeren

| Methode | Endpoint |
| --- | --- |
| GET | `/connectors` |
| GET | `/connectors/configs` |
| GET | `/connectors/file-uploads/runs` |
| POST | `/connectors/file-uploads/dispatch` |
| POST | `/connectors/file-uploads/dispatch/{run_id}/confirm` |
| POST | `/connectors/file-uploads/inspect` | Optionele providerdetectie vóór de wizard |

### Exporters

| Methode | Endpoint |
| --- | --- |
| GET | `/destinations` |
| POST | `/destinations` |
| PATCH | `/destinations/{destination_id}` |
| DELETE | `/destinations/{destination_id}` |
| POST | `/destinations/{destination_id}/activate` |
| POST | `/destinations/{destination_id}/test` |
| POST | `/destinations/{destination_id}/preview` |
| POST | `/destinations/{destination_id}/run` |
| POST | `/destinations/{destination_id}/{action}` |
| POST | `/destinations/{destination_id}/actual-budgets` |
| POST | `/destinations/{destination_id}/jupyter-key/rotate` |
| GET | `/destinations/{destination_id}/jupyter-notebook` |

### Sync Runs

| Methode | Endpoint |
| --- | --- |
| GET | `/sync-schedules?limit=500` |
| GET | `/sync-runs?limit={limit}&offset={offset}&connector={connector}&status={status}` |
| GET | `/sync-schedules/{schedule_id}` |
| GET | `/sync-schedules/{schedule_id}/preview?count=3` |
| POST | `/sync-schedules/preview` |
| PATCH | `/sync-schedules/{schedule_id}` |
| POST | `/sync-schedules/{schedule_id}/enable` of `/disable` |
| POST | `/sync-schedules/{schedule_id}/reset` |

### Holdingnieuws

| Methode | Endpoint |
| --- | --- |
| GET | `/holding-relevance/feed` met filters/paginering |
| GET | `/holding-relevance/calendar?limit=100` |
| GET | `/holdings?limit=500` |
| GET | `/accounts?limit=200` |
| GET | `/holding-relevance/notifications/preferences` |
| PUT | `/holding-relevance/notifications/preferences` |
| POST | `/holding-relevance/clusters/{cluster_id}/ack` |

Bronlinks in de holdingfeed zijn externe provider-URL's en openen bewust in een nieuw tabblad. Ze zijn geen finance-sync API-routes.

## Logging- en foutafhandeling

- API-calls gebruiken één auth-helper; HTTP 401 leidt naar `/login`.
- Fouten tijdens laden worden inline weergegeven met een retry-actie.
- Muterende acties tonen een busy-status en een succes-/foutresultaat.
- API-actie-links uit het control plane worden eerst naar een GUI-sectie vertaald; directe navigatie naar `/api/v1/...` is geblokkeerd.
- De GUI logt geen credentials; connectorcredentials worden alleen als gemaskeerde/stored-status weergegeven.

## Tijdelijke embedded-compatibiliteit

De actieve dashboardflow gebruikt uitsluitend `Importeren` en de providerneutrale
dispatch-endpoints. De oude globale functies `openCreateWizard`,
`openConfigModal` en `initDegiroWizard` blijven tijdelijk als JavaScript-
compatibiliteitslaag beschikbaar voor mogelijke embedded dashboardclients. De
DEGIRO-DOM-tree, automatische initialisatie en de oude Saxo-modal bestaan niet
meer. Nieuwe UI-code mag deze functies niet gebruiken; wijzigingen aan hun
contract moeten de GUI-regressietests tegelijk bijwerken.

## Auditstatus

Gecontroleerd met de GUI-template, OpenAPI-routecatalogus, JavaScript-syntax, gerichte GUI/control-plane/data-health-tests en een live dashboard smoke-test. Een interactieve browserkliktest was in de auditomgeving niet beschikbaar; die moet aanvullend worden uitgevoerd zodra de browserconnector verbonden is.
