# Provider-onafhankelijk spendingmodel

Finance Sync gebruikt `CanonicalTransactionData` als provider-onafhankelijke
laag. Providerwaarden worden niet vervangen door de normalisatie: de
canonical transactie bevat zowel de normale velden als `original_type`,
`original_status`, een `source_record_hash` en een versioned
`provider_metadata_contract`.

## Capabilities

Connectors publiceren optionele capabilities met een beschikbaarheidsniveau:
`complete`, `partial`, `incremental`, `historical` of `detail_only`. Een
ontbrekende capability is geldig en mag geen lege of verzonnen data opleveren.
De catalogus exposeert deze informatie als `spending_capabilities`.

## Classificatie en provenance

Merchant keys worden genormaliseerd buiten de provider-ID om. Een
`CategorySuggestion` bevat altijd bron, confidence en optionele taxonomie.
Merchant- en category mappings zijn tenant-scoped; destination-category-ID's
staan niet in het canonical transactiemodel. `TransactionOverride` bewaart
handmatige keuzes met actor en provenance.

## Splits, annotaties en lifecycle

Splits zijn niet-destructieve componenten van de oorspronkelijke transactie.
Annotaties slaan alleen veilige referenties, hashes, MIME-type, eigenaar en
retentie op. Lifecycle-events (`create`, `update`, `reverse`, `refund`,
`split`, `delete`, `tombstone`) zijn append-only en hebben een
tenant-/transactie-scoped idempotency key.

## Destinations

`exporter/capabilities.py` bevat de capabilitymatrix voor Wealthfolio, Actual
Budget, YNAB en Firefly III. Adapters behouden hun native semantiek en mogen
geen categorie-ID's van een andere bestemming hergebruiken. Destination
object-ID's worden opgeslagen in `DestinationObjectReference` en zijn nooit
bron-ID's.

Ruwe payloads en attachment-inhoud horen niet in dit model; privacy- en
retentiebeleid bepalen welke geselecteerde metadata überhaupt wordt bewaard.

## Beheer en reconciliation

Merchant- en category mappings, destination-neutrale rules, user overrides,
manual splits en lifecycle-events zijn tenant-scoped API-resources. User acties
dragen actor en `user_override` provenance; broncorrecties dragen
`source_correction`; destination enrichment gebruikt
`destination_enrichment`. De destination-reconciliation endpoint accepteert
de native read-resultaten van een adapter en rapporteert ontbrekende, dubbele
of inhoudelijk afwijkende objecten zonder één van beide kanten te wijzigen.
