---
title: "Breid Data health uit met datamodel-integriteit en downstream-pariteit"
status: complete
priority: 50
---

## Actuele implementatiestatus — 2026-09-06

### Afgerond in deze slice

- [x] Security-bearing transferlegs worden deterministisch gepaird op een
      allowlisted provider-/counterpartyreferentie, security, valuta, datum,
      quantity, tegengestelde cashrichting en verschillende accounts.
- [x] Tax-lots hebben een tenant-scoped `transfer_transaction_id` met
      migratie `0065`; destination-lots behouden de oorspronkelijke
      acquisition date, FIFO-basis en cost-basis-per-unit zonder een valse
      purchase-link te creëren.
- [x] Transferbasis wordt atomair en idempotent verplaatst: volledige en
      gedeeltelijke FIFO-migraties sluiten of verminderen de bronlotbasis,
      terwijl onvoldoende bronbasis geen gedeeltelijke database-mutatie
      achterlaat en via compute-statistieken als evidence gap zichtbaar is.
- [x] Data Health accepteert transfer-origin lots als geldige basis en
      signaleert ontbrekende, verkeerde of niet-security-matching
      transfer-origin transacties afzonderlijk in de tax-lotintegriteits-
      evidence.
- [x] Tax-lot reconstructie verwerkt security transfers naast purchases,
      sales en quantity-events; de verwerkingsvolgorde is deterministisch op
      occurred-at plus transaction-ID.

- [x] `DataHealthCategory` uitgebreid met `account_identity_conflict` en
      `duplicate_transaction_identity`.
- [x] `DataHealthIssue` uitgebreid met connection/account/security-context,
      record counts, blocking/repair flags en privacyveilige evidencevelden.
- [x] Deterministische issue-ID's toegevoegd op basis van een stabiele SHA-256
      digest; geen request-specifieke UUID's.
- [x] Account identity health toegevoegd aan `DataHealthService`.
- [x] Actieve `selected_accounts`-configuraties worden nu tegen de lokale,
      connection-scoped accountdataset gecontroleerd; ontbrekende geselecteerde
      provider-ID's leveren een niet-blokkerende GUI-finding zonder de
      encrypted credentialpayload te lezen.
- [x] Actieve accounts met een ontbrekende of provider-mismatched
      `connection_id` leveren nu een afzonderlijke, tenant-scoped
      account-identity finding; legacy accounts zonder connection blijven
      bewust buiten deze orphan-check.
- [x] De drie connection/account/delivery-controles hebben nu een echte
      PostgreSQL-regressietest: ontbrekende `selected_accounts`, een account
      naar een niet-bestaande connector en een tombstoned transactie binnen
      de eerder geëxporteerde activity-scope worden tenant-scoped en zonder
      secret- of remote payloaddata geprojecteerd.
- [x] Detectie toegevoegd voor meerdere actieve Trading212-accounts binnen
      één connection en voor de legacy fallback-ID `trading212` naast een
      actuele account-ID.
- [x] Detectie toegevoegd voor dezelfde provider/external transaction-ID die
      meerdere keren in de tenant voorkomt.
- [x] Detectie toegevoegd voor hergebruikte provider fingerprints; de ruwe
      fingerprint wordt niet in de GUI of evidence teruggegeven.
- [x] Generieke account-identity matching toegevoegd voor een beperkte
      allowlist van stabiele provider-metadata (`iban`, account/reference-ID's).
      De vergelijking normaliseert alleen scalar waarden, is tenant-scoped en
      toont uitsluitend een metadata-key en hash; er wordt niets automatisch
      gemerged.
- [x] De account-metadata allowlist herkent nu ook provider-equivalenten
      `account_id`/`accountId`/`accountID`, client- en brokerage-account-ID's
      en groepeert verschillende schrijfwijzen onder één semantische
      identity-key. Accounts met meerdere equivalente metadata-velden worden
      binnen één finding deduplicated, zonder vrije metadata-recursie of
      secretwaarden te exposen.
- [x] Dezelfde beperkte allowlist ondersteunt nu ook expliciete
      `monetary_account_id`/`monetaryAccountId`, `bank_account_id`/
      `bankAccountId` en `portfolio_id`/`portfolioId`-aliases; alleen scalar
      waarden worden genormaliseerd en gehasht. PostgreSQL-tenantisolatie is
      voor deze aliases afzonderlijk regressiegetest.
- [x] Detectie toegevoegd voor transacties met een ontbrekende account of een
      afwijkende provider/connector-relatie.
- [x] Detectie toegevoegd voor orphaned/mismatched `SyncCursor`-records.
- [x] Detectie toegevoegd voor runs die `completed` zijn maar `failed`,
      `skipped` of warnings in het runrapport bevatten.
- [x] Succesvolle sync-runs rapporteren nu naast aantallen ook de
      resource-identiteiten in `report.account_external_ids`. Data Health
      vergelijkt de nieuwste run per connection met `selected_accounts` en
      maakt ontbrekende provideraccounts zichtbaar als `partial_sync`; oude
      runs zonder dit veld worden bewust niet als fout geïnterpreteerd.
- [x] Detectie toegevoegd voor een `SyncCursor` die achterloopt op de laatste
      succesvolle run van dezelfde connection; dit is een niet-blokkerende,
      tenant-scoped `partial_sync`-finding met resource-, cursor- en
      run-watermarkdetails, zonder automatische cursorwijziging. De
      referentiewatermark wordt via een connection-scoped `MAX(cursor)` bepaald
      en is daardoor niet afhankelijk van de limiet op detailruns.
- [x] Eerste holdings-versus-transacties quantity-check toegevoegd voor
      purchase/sale-activiteiten versus de laatste holding-snapshot.
- [x] Holdings-versus-transacties quantity-check uitgebreid met security-
      bearing transfers; positieve transferbedragen zijn inbound en negatieve
      transferbedragen outbound.
- [x] Transfer-integriteit signaleert nu ook ontbrekende en nulbedragen als
      blocking `incomplete_transaction`-finding; de lightweight canonical
      adapter behoudt zowel deze semantic finding als de eventuele
      `unbalanced_transfer`-finding. Data health projecteert de twee categorieën
      apart, zodat een ontbrekende richting niet als alleen een ontbrekende
      tegenboeking wordt gelabeld. Transfer-findings worden uit de generieke
      cash-contracttelling gehouden, zodat hetzelfde record niet dubbel in de
      canonical overview verschijnt.
- [x] De shared activity-validator accepteert alleen ASCII-vormige
      drieletterige uppercase ISO-4217-codes en behandelt niet-finite of
      niet-numerieke transferbedragen (`NaN`, onparsebare tekst) als blocking
      incomplete activity; hierdoor kan de transfer-pairing niet op ongeldige
      Decimal-waarden crashen.
- [x] Trade-semantiek blokkeert nu ook niet-numerieke of niet-positieve
      quantities en negatieve/niet-numerieke unit prices. De canonical SQL
      aggregate telt deze findings mee in `contract_invalid_total_count`, zodat
      Data health en Wealthfolio-preflight dezelfde trade-integriteitsomvang
      rapporteren.
- [x] Portfolio quantity- en cashreconciliatie gebruiken nu finite Decimal-
      parsing voor persisted numerieke waarden. Ongeldige activitybedragen of
      quantities laten de overview niet meer crashen; de canonical activity-
      preflight blijft eigenaar van de blocking semantic finding.
- [x] Quantity-check uitgebreid met expliciete split/adjustment/corporate-
      action ratio's uit privacyveilige provider metadata
      (`split_ratio`/`quantity_multiplier`).
- [x] Quantity-events zonder voorafgaande opening quantity-basis worden niet
      langer als `0 × ratio` doorgerekend; ze rapporteren een niet-blokkerende
      `insufficient_evidence`-finding in plaats van een false-positive mismatch.
- [x] Niet-modelleerbare corporate actions rapporteren nu een niet-blockerende
      `invalid_activity_semantics`-issue met `insufficient_evidence` in plaats
      van een verzonnen quantity-mutatie.
- [x] Quantity-mismatches rapporteren naast de absolute afwijking ook het
      afwijkingspercentage en de laatste activity- en holding-snapshotdatum.
- [x] `quantity_event_ratio()` leest nu zowel de legacy flat metadata-shape
      als het persisted `ProviderMetadata.fields`-contract, inclusief veilige
      numerator/denominator-ratio's.
- [x] Quantity-event-normalisatie ondersteunt daarnaast een beperkte allowlist
      voor geneste eventvelden en `new/old quantity`-equivalenten, zonder vrije
      provider-metadata recursief te interpreteren.
- [x] De shared `quantity_event_ratio()` normaliseert nu ook expliciete ratio-
      strings (`2:1`, `1/10`, `for`, `op`) in persisted flat- of envelope-
      metadata, waardoor legacy/providervelden dezelfde downstream-semantiek
      behouden als connector-parsing.
- [x] DeGiro statement-normalisatie classificeert bekende corporate-action-
      beschrijvingen als `corporate_action` in plaats van als `fee`, zodat
      Data health ze als quantity-event/evidence-gap kan beoordelen.
- [x] Saxo statement-normalisatie classificeert `Corporate action`/`Corporate
      actie` vóór dividendherkenning als hetzelfde canonical
      `corporate_action`-type.
- [x] Saxo-labels met een expliciete corporate-action-ratio (`2:1`, `1/10`,
      `for`, `op`) vullen veilig `ProviderMetadata.fields.split_ratio`; labels
      zonder ratio blijven een expliciete evidence gap.
- [x] DeGiro corporate-actionlabels met een expliciete ratio (`2:1`, `for`,
      `op`) worden opgeslagen als canonical `ProviderMetadata.fields.split_ratio`;
      labels zonder ratio blijven bewust een evidence gap.
- [x] Trading212 bekende split/corporate-actiontypen worden als
      `corporate_action` genormaliseerd en expliciete ratio-velden worden in
      hetzelfde canonical metadata-contract opgeslagen.
- [x] Trading212 ratiovelden accepteren nu ook expliciete provider-notaties
      `2:1`, `1/10`, `for` en `op`, naast numerieke ratio's en
      `new/old`-quantityvelden; alle vormen worden veilig naar één positieve
      `ProviderMetadata.fields.split_ratio`-waarde genormaliseerd.
- [x] `corporate_action` is nu ook een echte `TransactionType`-waarde; de
      sync-persistence valt dit type niet meer terug naar `other` en de
      Wealthfolio-mapper projecteert het expliciet als `ADJUSTMENT`.
- [x] De Wealthfolio-row behoudt voor `corporate_action` ook de expliciete
      subtype/provenance `CORPORATE_ACTION`, zodat downstream reconciliatie
      het event niet alleen als generieke adjustment ziet.
- [x] Trading212 parsed transactions testen nu ook end-to-end dat een expliciete
      `newQuantity`/`oldQuantity`-ratio in `ProviderMetadata.fields` terechtkomt.
- [x] Trading212 accepteert tijdens parsing ook `newUnits`/`oldUnits`, zodat
      connector- en shared-validator-allowlists identiek blijven.
- [x] De shared quantity-event validator ondersteunt ook `newUnits`/
      `oldUnits`-equivalenten in het persisted metadata-contract.
- [x] De canonical Data health-projectie valideert nu ook `split`,
      `adjustment` en `corporate_action` via dezelfde shared preflight-stream;
      het persisted `provider_metadata_contract` wordt daarbij doorgegeven,
      zodat ontbrekende ratio-evidence niet stil uit de downstream-check valt.
- [x] De tax-lot compute-response rapporteert nu hoeveel quantity-events zijn
      toegepast, hoeveel lots zijn aangepast en hoeveel events door ontbrekende
      ratio-evidence zijn overgeslagen.
- [x] Tax-lot reconstructie wist bestaande lots tenant-scoped in de service
      zelf, zodat directe callers en de GUI geen dubbele lotgeneraties kunnen
      opbouwen.
- [x] Resterende directe relationship-, cursor-, duplicate-account- en
      provider-revision-findings rapporteren nu expliciet `total_count` en
      `detail_limit` in hun evidence.
- [x] Duplicate transaction-ID-, fingerprint- en semantische-duplicate-groepen
      rapporteren dezelfde count-contractvelden zonder gevoelige detailrijen
      te materialiseren.
- [x] Cash-reconciliatie-findings (snapshot mismatch, flow mismatch en
      ontbrekende snapshot) leveren eveneens `total_count` en `detail_limit`.
- [x] Tax-lot reconstructie past expliciete split/adjustment/corporate-action
      ratio's toe op open lots: quantity schaalt mee, `cost_basis_total` blijft
      behouden en `cost_basis_per_unit` wordt met Decimal door de ratio gedeeld.
- [x] Regressietest toegevoegd voor de volledige canonical → shared-validator
      projectie: dezelfde fee-semantiek levert in Data health dezelfde finding
      op als vóór Wealthfolio-export.
- [x] Canonical transfer-, zero-cost- en activity-order-checks gebruiken het
      gedeelde `validate_transaction_stream()`-contract; meerdere findings op
      één activiteit blijven afzonderlijk en deterministisch zichtbaar.
- [x] Cashaccounts zonder bruikbare provider balance-snapshot rapporteren een
      niet-blokkerende `insufficient_evidence`-finding met valuta en veilige
      accountcontext in plaats van stil te worden overgeslagen.
- [x] De gedeelde activity-validator blokkeert nu ook trades zonder security,
      controleert de ISO-4217-vorm van aanwezige valutacodes en valideert
      positieve fee/tax-waarden inclusief feevaluta vóór Wealthfolio-export.
- [x] Cross-currency trades zonder positieve FX-rate geven vóór export een
      niet-blokkerende `invalid_activity_semantics`-warning; de canonical
      activity-query projecteert dezelfde evidence wanneer security- en
      activityvaluta beschikbaar zijn.
- [x] Canonical Data health projecteert deze security-, valuta- en fee/tax-
      contractfindings via dezelfde validator; legacy lichte query-rows blijven
      backwards-compatible en tonen geen ruwe providerpayloads.
- [x] Activities zonder stabiele `external_transaction_id` blokkeren de
      Wealthfolio-preflight en worden in de volledige canonical activity-query
      als privacyveilige, deduplicated Data-health finding geprojecteerd.
- [x] De lightweight transfer-query adapter pairt tegengestelde legs op
      datum, valuta en absolute hoeveelheid voordat hij de shared validator
      aanroept; bestaande legitieme transferparen worden daardoor niet als
      false-positive warnings geprojecteerd.
- [x] Transfer-pairing in de volledige shared validator leest nu zowel flat
      metadata als het persisted `ProviderMetadata.fields`-envelope; een
      security-bearing transfer zonder quantity wordt als blocking incomplete
      activity gemarkeerd vóór Wealthfolio-export.
- [x] Tax-lotintegriteit vergelijkt nu ook cumulatieve sales met de beschikbare
      lotbasis per account/security en blokkeert onmiskenbare oversells;
      situaties zonder lotbasis blijven bewust buiten de berekening.
- [x] Tax lots zonder `purchase_transaction_id` worden nu als
      niet-blokkerende evidence-gap gemeld; lots met structurele fouten of
      oversells blijven blocking.
- [x] Security-integrityprojecties blijven model-conform tenant-safe: gedeelde
      securityrecords worden alleen via tenant-scoped holding/transaction-
      references meegenomen; ongebruikte records lekken niet naar Data health.
- [x] De incomplete-tradeprojectie rapporteert nu het volledige window-count
      afzonderlijk van de maximaal 100 GUI-details via privacyveilige evidence
      (`total_count` en `detail_limit`).
- [x] Dezelfde `total_count`/`detail_limit`-evidence is toegevoegd aan
      incomplete holdings en incomplete, tenant-referenced securities.
- [x] Wealthfolio incomplete-valuation en incomplete-cost-basis checks
      rapporteren eveneens het volledige window-count naast maximaal 1000
      detailrecords.
- [x] Partial-sync findings gebruiken nu een tenant-scoped SQL-count voor
      completed runs met failed/skipped/warnings en tonen maximaal 100 details
      naast `total_count`/`detail_limit` evidence.
- [x] Incomplete import-runs rapporteren naast hun per-run impact ook het
      tenant-scoped aantal getroffen imports en een detail-limiet van 20.
- [x] Canonical activity-findings worden vóór projectie deduplicated en
      deterministisch gesorteerd op record, categorie, severity en boodschap;
      herhaalde query-rows verhogen daardoor niet opnieuw impact counts of
      wijzigen stabiele issue-digests.
- [x] Direct geaggregeerde negative-balance, transfer, quote-failure en
      negative-history issues rapporteren nu expliciet `total_count`,
      `detail_limit` en `affected_record_count`, zodat GUI-details begrensd
      blijven zonder het impactaantal te verliezen.
- [x] Destination parity-accountresultaten gebruiken typed
      `DestinationParityAccount`-modellen en de secretvrije parity-metrics
      gebruiken het expliciete integercontract; losse parity-accountdicts zijn
      uit de nieuwe probeprojectie verwijderd.
- [x] De bestaande connector/account-deleteflow voldoet aan de repairgrens:
      tenant-scoped preview, expliciete GUI-confirmatie, transactionele
      cascade en lifecycle-audit; Data health start deze destructieve flow
      niet automatisch.
- [x] Quantity-evidence voor niet-modelleerbare corporate actions en
      tax-lotintegriteit bevat nu bounded detailcontext plus afzonderlijke
      total-counts voor broken lots, oversold groepen en ontbrekende
      aankoopbasis.
- [x] Zero-cost trade- en invalid-order projecties gebruiken filtered
      PostgreSQL window-counts naast hun begrensde canonical detailwindow;
      lightweight query-rows blijven backwards-compatible.
- [x] Trade-activiteiten zonder security gebruiken nu eveneens een
      tenant-scoped filtered window-count voor de incomplete activity-contract
      finding, met maximaal 100 details en een volledig impactaantal.
- [x] Invalid activity-contract findings tellen nu afzonderlijke validator-
      signalen via een PostgreSQL window aggregate voor ontbrekende externe
      ID, ongeldige ISO-valuta en ontbrekende FX-rate; de 100-row detailwindow
      blijft alleen presentatie-evidence.
- [x] De canonical activity-projectie omvat nu ook fee/tax- en cash-types;
      positieve fee/tax-waarden, fee-valuta en ontbrekende cashbedragen worden
      vóór export door dezelfde validator en aggregate-counts afgedwongen.
- [x] De samengestelde Data health-overview sorteert findings deterministisch
      op severity, categorie en stabiele issue-ID, zodat de GUI-volgorde niet
      afhankelijk is van de volgorde van losse projecties.
- [x] Wealthfolio preflight signaleert holdings met een aanwezige maar niet-
      verifieerbare cost basis wanneer er geen open tax lot of aankoop-
      transactiegrondslag bestaat; dit blijft een warning met bounded evidence
      en blokkeert bestaande handmatige cost-basiswaarden niet.
- [x] Cash reconciliation rekent nu, wanneer meerdere balance-snapshots
      beschikbaar zijn, de openingssnapshot plus tenant-scoped canonical
      cashactiviteiten door naar de volgende snapshot. Afwijkingen krijgen
      afzonderlijke flow-evidence; accounts zonder actuele snapshot blijven
      `insufficient_evidence`.
- [x] Gedeelde `validate_activity_semantics()` toegevoegd voor Data health en
      Wealthfolio preflight: tradevelden, quantity-event-ratio's en logische
      `occurred_at`/`booked_at`-volgorde worden vanuit dezelfde regels getoetst.
- [x] Security identity health toegevoegd voor tenant-referenced securities:
      malformed ISIN/valuta (inclusief niet-alfanumerieke drielettercodes)
      blokkeren export en ticker+valuta+type met meerdere ISIN's levert een
      warning met security-context op.
- [x] Security identity health verrijkt cross-variant tickerambiguïteiten met
      aanwezige `SecurityListing` MIC/venue-evidence. Wanneer listingdata
      ontbreekt blijft de bestaande niet-blokkerende warning intact; automatische
      instrumentmerge blijft uitgesloten.
- [x] Shared activity validator uitgebreid met de bestaande zero-cost trade
      warning, zodat Wealthfolio preflight dezelfde semantische classificatie
      gebruikt als de canonical Data health-regel.
- [x] Lokale Wealthfolio destination-paritycheck toegevoegd op basis van de
      laatste tenant-scoped `ExportRun` en het preflight/post-export manifest;
      partial delivery en degraded delivery worden als `destination_drift`
      gemeld.
- [x] Destination parity uitgebreid met tenant-scoped
      `WealthfolioAccountMapping`-validatie; incomplete mappings zijn
      warnings en conflicterende provider-account-id's zijn blocking.
- [x] Timeout-begrensde opt-in `probe_wealthfolio_destination()` toegevoegd;
      de helper classificeert remote status als `ready`, `unauthorized` of
      `unavailable` en retourneert alleen parity-inputs (accounts/assets).
- [x] Bestaande expliciete `POST /destinations/{target_id}/test`-actie
      gekoppeld aan de probe; Wealthfolio remote account mappings worden
      binnen de tenant/target ownership boundary gecontroleerd en ontbrekende
      mappings geven een `degraded` bestemming.
- [x] De opt-in destination-test vergelijkt nu ook canonical transaction
      IDs met remote Wealthfolio activities via `sourceRecordId`/
      `externalTransactionId`; ontbrekende remote activities geven
      `degraded` parity.
- [x] Remote asset parity toegevoegd aan de expliciete destination-test;
      tenant-referenced canonical securities worden op ISIN/ticker tegen het
      remote assetcatalogus gecontroleerd en ontbrekende assets geven
      `degraded` parity.
- [x] `unauthorized` en `unavailable` probe-resultaten worden als aparte
      persisted destination health-status opgeslagen in plaats van als
      generieke parity mismatch.
- [x] Persisted Wealthfolio target-health (`degraded`, `failed`,
      `unauthorized`, `unavailable`) wordt nu tenant-scoped als actionable
      `destination_drift` in Data health geprojecteerd, met een directe
      `test_destination`-actie.
- [x] Data health GUI uitgebreid met compacte destination-paritycontext:
      remote status en lokale delivery-aantallen worden weergegeven zonder
      remote payloads of secrets te tonen.
- [x] `TestResponse` van de expliciete Wealthfolio destination-test uitgebreid
      met privacyveilige parity-counts (`remote_accounts`, `remote_assets`,
      `remote_activities`, `canonical_activities`); een contracttest bewaakt
      dat geen connectorcredentials of remote payloads worden teruggegeven.
- [x] Wealthfolio provider-account identity-normalisatie gecentraliseerd voor
      camelCase (`providerAccountId`) en snake_case (`provider_account_id`),
      zodat mapping- en activity-parity dezelfde remote account-ID gebruiken.
      Regressietests dekken beide API-spellingen en de precedence bij beide.
- [x] Expliciete Wealthfolio destination-test blokkeert nu ook mappings zonder
      `provider_account_id`: een incomplete of remote ontbrekende mapping kan
      niet langer onterecht als `ready` eindigen en wordt als `degraded`
      opgeslagen.
- [x] PostgreSQL-integratietest toegevoegd voor tenant-isolatie van persisted
      destination health en Wealthfolio-account mappings; cross-tenant health
      errors en mapping-details lekken niet naar Data health.
- [x] Wealthfolio mapping-conflicten worden nu per `target_id` gegroepeerd.
      Dezelfde provider-account-ID op twee onafhankelijke destinations levert
      geen false positive meer op; een dubbele koppeling binnen één target
      blijft blocking en krijgt target-scoped evidence en issue-ID.
- [x] De canonical zero-cost trade-check gebruikt nu de gedeelde
      `validate_activity_semantics()` voor de semantische classificatie en
      voorkomt dat een incomplete trade daarnaast opnieuw als zero-cost wordt
      geprojecteerd; de bestaande privacyveilige issuecategorie blijft gelijk.
- [x] De canonical activity-flow projecteert nu ook een ongeldige
      `booked_at < occurred_at`-volgorde als blocking
      `invalid_activity_semantics`, met stabiele issue-ID, record count,
      transactiereferenties en een veilige `view_transactions`-actie.
- [x] Destination-probe regressiedekking uitgebreid voor `unauthorized`,
      `unavailable` door remote request failure en timeout; de probe geeft
      alleen gesaniteerde status/redenen terug en lekt geen exceptionpayload.
- [x] Expliciete destination-testrespons uitgebreid met privacyveilige
      `parity_accounts`-samenvattingen per canonical account
      (`canonical_activities`, `remote_activities`, `missing_activities`);
      de destination-wizard toont deze aantallen compact met een afgekorte
      canonical account-ID en nooit remote payloads of secrets.
- [x] Remote Wealthfolio parity uitgebreid met counts voor unmapped remote
      accounts en stale remote activiteiten. Canonical tombstones blijven
      onderdeel van de vergelijking zodat lokaal ingetrokken activiteiten niet
      stilzwijgend downstream blijven staan; de opt-in test retourneert dan
      `degraded` zonder remote data te schrijven.
- [x] Data health detecteert lokaal read-only wanneer een tombstoned
      transaction al binnen een Wealthfolio delivery-scope valt; de finding
      vergelijkt de activitydatum met de laatst geleverde scope (niet de
      latere tombstone-aanmaakdatum), vraagt remote verificatie en start geen
      automatische verwijdering.
- [x] PostgreSQL-backed HTTP-contracttests toegevoegd voor de volledige
      `POST /destinations/{target_id}/test`-flow: `ready`, `unauthorized`,
      `unavailable`, unmapped remote accounts en stale tombstoned activities.
      De tests gebruiken echte JWT-auth, tenant-scoped PostgreSQL-opslag en
      controleren ook de persisted `last_health_status` zonder secrets in de
      response.
- [x] Aggregate remote parity evidence wordt nu persistent opgeslagen op
      `ExportTarget.last_parity_summary` via migratie `0064`. Alleen een
      allowlist van niet-negatieve counts en de status wordt bewaard; Data
      health projecteert deze counts veilig en negeert onverwachte JSON-velden.
- [x] De Data health-issuekaart toont de persistente parity-samenvatting in
      een compacte, uitklapbare detailweergave met remote/canonical counts.
      De GUI gebruikt alleen allowlisted aggregates en toont geen remote
      payloads, secrets of raw account-identifiers.
- [x] Remote destination probes zijn voorzien van de configureerbare flag
      `DESTINATION_REMOTE_PROBE_ENABLED` en een target/credential-scoped
      rate-limit (`DESTINATION_PROBE_RATE_LIMIT_MAX_REQUESTS` plus window).
      Redis wordt gebruikt voor gedeelde limiting; zonder Redis valt de
      dependency terug op een bounded in-memory sliding window. Een disabled
      probe maakt geen remote call en geeft een expliciete `disabled` status.
- [x] Probe-observability toegevoegd via Prometheus counter, duration
      histogram en allowlisted parity gauges. Labels bevatten uitsluitend
      destination-type, status en vaste metricnaam; target-ID's, accounts,
      bedragen, secrets en remote payloads komen niet in metrics terecht.
- [x] Destination parity-response getypeerd met expliciete Pydantic-modellen
      voor aggregate `DestinationParityCounts` en
      `DestinationParityAccount`; hierdoor worden JSON-validatie en OpenAPI
      niet langer gedragen door vrije dictionaries.
- [x] Semantische transaction-duplicatecheck toegevoegd voor records zonder
      bruikbare provider-ID (ook whitespace-only IDs), gegroepeerd op tenant,
      provider, account, datum, type, bedrag/valuta, quantity en security.
      Alleen een privacyveilige semantic-key-hash wordt geëvidenceerd.
- [x] Basis tax-lot-integriteitscheck toegevoegd voor negatieve, overschreden
      en gesloten positieve resterende hoeveelheden.
- [x] Cashreconciliatie toegevoegd tussen `Account.current_balance` en de
      meest recente `current`/`cash`/`booked` balance-snapshot in dezelfde
      valuta, met een Decimal-tolerantie van EUR 0,01.
- [x] Tax-lotvalidatie uitgebreid met account/security-existence en
      purchase/sale transaction-link checks.
- [x] Tax-lot transaction-links valideren nu ook de transactietypen zelf:
      `purchase_transaction_id` moet naar een canonical purchase wijzen en
      `sale_transaction_id` naar een sale, naast tenant/account/security-
      consistentie.
- [x] Tax-lot state-validatie detecteert nu ook een open lot met positieve
      oorspronkelijke quantity en nul resterende quantity als structurele
      fout; legacy gesloten lots zonder sale-link blijven backwards-compatible
      beoordeeld op hun bestaande quantity/closed-state-regels.
- [x] Tax-lot state-validatie blokkeert nu ook lots met nul of negatieve
      oorspronkelijke quantity als `non_positive_quantity`; gewone holding-
      snapshots met quantity nul vallen buiten deze lotregel.
- [x] Tax-lotvalidatie blokkeert nu ook negatieve of niet-numerieke
      cost-basiswaarden en ongeldige lot-valuta; een geldige nul-cost-basis
      blijft toegestaan. De controle is ook met tenant-scoped PostgreSQL-
      records gevalideerd. De relatie tussen `cost_basis_total` en
      `cost_basis_per_unit × quantity` wordt met een Decimal-centtolerantie
      gecontroleerd.
- [x] Tax-lotvalidatie behandelt ook niet-finite of onparsebare lot- en
      resterende hoeveelheden veilig als blocking structurele fout. De finding
      bewaart een reason-coded `quantity_error_count` en detail (`invalid_quantity`
      of `invalid_remaining_quantity`) zonder de Decimal-berekening te laten
      crashen.
- [x] Tax-lotvalidatie meldt nu ook verkopen met een niet-finite of
      onparsebare quantity als blocking fout. De finding bewaart
      `invalid_sale_quantity_count` en veilige transaction/account/security-
      context; zulke verkopen verdwijnen niet langer stil uit de lotcapaciteits-
      berekening.
- [x] Verkopen waarvoor geen enkele lotbasis bestaat leveren nu een
      niet-blokkerende `insufficient_evidence`-finding op in plaats van stil
      te worden genegeerd; echte oversells blijven blocking.
- [x] Tax-lot findings bevatten nu per gemarkeerd lot een reason-coded detail
      voor cost-basisfouten, zodat de GUI niet alleen een totaal maar ook de
      concrete herstelrichting toont.
- [x] PostgreSQL-integratietest toegevoegd voor tenant-isolatie en
      connection-scoping van account- en transaction-identity checks.
- [x] PostgreSQL-integratietests toegevoegd voor tenant-isolatie van
      sync-cursor/run-integrity, cashreconciliatie en portfolio-quantity.
- [x] Regressietests toegevoegd voor de Trading212 legacy/current-situatie en
      cross-connection transaction identity, fingerprint privacy en
      account/connector-relaties.
- [x] Outbox-migratie rollback/fresh-schema robuuster gemaakt: migratie 0063
      gebruikt `DROP INDEX IF EXISTS` en de eerdere pending-index-recreatie is
      idempotent, zodat integration-migrations ook na downgrade/upgrade
      roundtrips werken.

### Verificatie van deze slice

- `uv run pytest -q tests/test_data_health.py tests/test_control_plane_api.py tests/test_openapi_connectors.py tests/test_gui_dashboard.py`
  → 121 passed.
- `uv run pytest -q tests/test_wealthfolio_preflight.py tests/test_data_health.py tests/test_control_plane_api.py tests/test_openapi_connectors.py tests/test_gui_dashboard.py`
  → 126 passed.
- `uv run pytest -q tests/test_data_health.py -k destination_parity`
  → 2 passed; partial delivery en incomplete account mapping worden gemeld.
- `uv run pytest -q tests/test_wealthfolio_preflight.py -k destination_probe`
  → 2 passed; probe haalt optioneel remote activity-inputs op binnen timeout.
- `uv run pytest -q tests/test_wealthfolio_preflight.py -k wealthfolio_assets`
  → 1 passed; asset identity match gebruikt ISIN en ticker als fallback.
- `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -q tests/test_wealthfolio_preflight.py tests/test_data_health.py`
  → 95 passed; nul-/ontbrekende en niet-numerieke transferbedragen,
  trade quantity/unit-price-validatie,
  ASCII ISO-4217-validatie, lightweight transferparen,
  deduplicated canonical transferprojectie, finite portfolio parsing,
  persisted ratio-string parsing, niet-finite tax-lot quantities en invalid
  sale quantities blijven groen.
- `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -q tests/test_data_health.py -k tax_lot`
  → 11 passed, 53 deselected; tax-lot quantity-, cost-basis-, relatie-,
  oversell- en evidence-gapchecks blijven groen.
- `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py`
  → 99 passed; de gecombineerde Data health- en Wealthfolio-preflightketen
  blijft groen na de tax-lot state-validatie.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'tax_lot_cost_basis'`
  → 1 passed, 9 deselected; tenant-scoped tax-lot cost-basis- en relatiechecks
  blijven groen.
- `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -q tests/connectors/trading212/test_trading212_connector.py`
  → 80 passed, 1 skipped; Trading212 parsing, duplicate-preventie en
  corporate-action ratioformats blijven groen.
- `make test-integration`
  → 193 passed, 3931 deselected, 22 bestaande warnings in 168.15s; de
  volledige PostgreSQL/Redis integration-suite blijft groen inclusief Data
  health, destination parity, migraties en Trading212 sync-idempotentie.
- `uv run ruff check src/finance_sync/services/wealthfolio_preflight.py src/finance_sync/services/data_health.py tests/test_wealthfolio_preflight.py`
  → groen; `git diff --check` → groen.
- `uv run pyright src/finance_sync/services/wealthfolio_preflight.py src/finance_sync/services/data_health.py`
  → 0 errors, 0 warnings.
- De transferprojectie en generieke activity-contractprojectie hebben
  verschillende ownership: incomplete transfers worden alleen via de
  dedicated transfercategorie gerapporteerd; overige cashactiviteiten blijven
  in het generieke contractpad.
- `uv run pytest -q tests/e2e/test_destinations_lifecycle.py`
  → 5 tests correct verzameld; lokaal overgeslagen door ontbrekende e2e-
  infrastructuur.
- `uv run pytest -q tests/test_data_health.py -k destination`
  → 4 passed; partial parity, incomplete mapping en persisted target-health
  worden als Data health-context geprojecteerd.
- `uv run pytest -q tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → 129 passed; GUI destination-paritycontext en bestaande contracts blijven
  groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → 146 passed; destination-testrespons, parity-counts, Data health,
  destination-probe, GUI en OpenAPI-contracts blijven groen.
- `uv run ruff check src/finance_sync/api/v1/destinations.py tests/test_destinations.py tests/test_data_health.py tests/test_gui_dashboard.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → groen; `git diff --check` → groen.
- `uv run ruff format tests/integration/test_control_plane_phase4_pg.py && uv run ruff check tests/integration/test_control_plane_phase4_pg.py`
  → groen.
- `uv run pytest -q -rs tests/integration/test_control_plane_phase4_pg.py`
  → 5 tests correct verzameld; lokaal overgeslagen omdat PostgreSQL en Redis
  niet zijn geconfigureerd (`TEST_DATABASE_URL`/`TEST_REDIS_URL` of
  `make test-integration` vereist).
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -vv`
  → 5 passed tegen echte PostgreSQL/Redis; tenant-isolatie van identity,
  sync-integrity, cash/quantity en destination parity is bevestigd.
- `uv run pytest -q tests/integration/test_migrations.py -k 'downgrade_base_removes_schema or upgrade_after_downgrade_roundtrip or 0036_0037_survive_downgrade_roundtrip'`
  → 3 passed; outbox-index rollback/recreate is idempotent.
- `make test-integration`
  → 181 passed, 3852 deselected, 22 warnings; volledige integration suite
  tegen de ephemeral PostgreSQL/Redis-stack is groen.
- `uv run pytest -q tests/test_data_health.py -k 'transaction_'`
  → 4 passed; provider-ID-, fingerprint-, relatie- en semantische duplicate
  checks blijven privacyveilig.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -vv`
  → 6 passed tegen echte PostgreSQL/Redis; semantic duplicate tenant-isolatie
  is bevestigd naast de bestaande phase-4 checks.
- `make test-integration`
  → 182 passed, 3853 deselected, 22 warnings; volledige integration suite
  blijft groen na semantic duplicate-detectie.
- `uv run pytest -q tests/test_data_health.py -k 'account_metadata_identity or transaction_'`
  → 5 passed; provider-metadata identity matching, privacy-redaction en
  transaction identity/semantic-duplicate checks blijven groen.
- `uv run ruff format src/finance_sync/services/data_health.py tests/test_data_health.py tests/integration/test_control_plane_phase4_pg.py`
  → 1 file reformatted; overige bestanden ongewijzigd.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py tests/integration/test_control_plane_phase4_pg.py`
  → groen.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -vv`
  → 7 passed tegen echte PostgreSQL/Redis; metadata-identity tenant-isolatie
  is bevestigd naast de bestaande phase-4 checks.
- `make test-integration`
  → 183 passed, 3854 deselected, 22 warnings in 151s; volledige integration
  suite blijft groen na metadata-identity matching.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → 158 passed, 2 warnings; unmapped/stale parity-counts, tombstone-aware
  activityvergelijking, destination wizard en bestaande UI/preflight-contracts
  blijven groen.
- `uv run ruff check src/finance_sync/api/v1/destinations.py tests/test_destinations.py src/finance_sync/services/data_health.py tests/test_data_health.py tests/integration/test_control_plane_phase4_pg.py`
  → groen; `git diff --check` → groen.
- `uv run pytest -q tests/test_destinations.py -k 'activity_parity or destination_test_response'`
  → 2 passed; missing canonical, unknown remote en lokaal getombstonede
  activiteiten worden als respectievelijk missing/stale geteld.
- `make test-integration`
  → 183 passed, 3855 deselected, 22 warnings in 155s; brede PostgreSQL/Redis
  regressiesuite blijft groen na de remote-parity uitbreiding.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_destinations_api_pg.py`
  → 5 passed; echte HTTP/JWT/PG-contracttests voor ready, unauthorized,
  unavailable, unmapped remote accounts en stale tombstoned activities.
- `uv run ruff check tests/integration/test_destinations_api_pg.py && git diff --check`
  → groen.
- `make test-integration`
  → 188 passed, 3855 deselected, 22 warnings in 157s; volledige integration
  suite inclusief de nieuwe destination HTTP-contracttests blijft groen.
- `uv run pytest -q tests/test_destinations.py tests/test_data_health.py`
  → 55 passed, 1 warning; persistent parity-evidence filtering en de bestaande
  destination/Data health-contracten blijven groen.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_destinations_api_pg.py`
  → 5 passed; HTTP/JWT/PG-contracttests bevestigen ook de persistent
  `last_parity_summary`-counts.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_migrations.py -k 'upgrade_head_creates_full_schema or single_head_linear_chain or upgrade_after_downgrade_roundtrip'`
  → 3 passed; migratie `0064` maakt en herstelt het nieuwe destination-veld
  binnen de bestaande migratieketen.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py`
  → 7 passed; Data health blijft tenant-scoped met het nieuwe optionele
  parity-summaryveld.
- `make test-integration`
  → 188 passed, 3855 deselected, 22 warnings in 164s; volledige suite opnieuw
  uitgevoerd tegen de actuele migratie-head `0064`.
- `uv run pytest -q tests/test_gui_dashboard.py -k 'destination_parity or dashboard_wizard_escaping'`
  → 2 passed; de uitklapbare persistente parity-samenvatting blijft HTML-
  escaped en payloadvrij.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → 158 passed, 2 warnings; destination parity, persistence, Data health en
  GUI-contracten blijven groen.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_destinations_api_pg.py -vv`
  → 7 passed; disabled probes maken geen remote call en target-scoped
  rate-limiting retourneert correct `429` naast ready/unauthorized/unavailable
  en parity-degradaties.
- `uv run ruff check src/finance_sync/api/middleware/destination_probe_rate_limit.py src/finance_sync/api/v1/destinations.py src/finance_sync/config/settings.py tests/integration/test_destinations_api_pg.py`
  → groen; `git diff --check` → groen.
- `uv run pytest -q tests/test_destination_metrics.py`
  → 1 passed; alleen vaste aggregate parity metrics worden geregistreerd en
  onverwachte/raw velden worden genegeerd.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_destinations_api_pg.py`
  → 7 passed; remote probe-statussen, feature flag, rate-limit en metric-
  registratie werken tegen de echte HTTP/PG/Redis-harness.
- `uv run ruff check migrations/versions/0009_sync_schema_to_orm.py migrations/versions/0063_claim_outbox_messages.py tests/integration/test_control_plane_phase4_pg.py`
  → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → 151 passed; incomplete mapping parity, destination-testrespons en de
  bestaande Data health/preflight/UI-contracts blijven groen.
- `uv run ruff check src/finance_sync/api/v1/destinations.py tests/test_destinations.py tests/test_data_health.py tests/test_gui_dashboard.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → groen; `git diff --check` → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → 152 passed; target-scoped mapping-conflict regression en alle bestaande
  destination/Data health/preflight/UI-contracts blijven groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/api/v1/destinations.py tests/test_data_health.py tests/test_destinations.py tests/test_gui_dashboard.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → groen; `git diff --check` → groen.
- `uv run pytest -q tests/test_wealthfolio_preflight.py tests/test_data_health.py`
  → 43 passed; de Data health-transferprojectie gebruikt nu dezelfde
  `validate_transaction_stream()`-regels als de Wealthfolio-preflight via een
  backward-compatible adapter voor de bestaande lichte query-rows.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen; de transferconvergentie introduceert geen lint- of whitespace-
  regressies.
- `uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py`
  → 44 passed; zero-cost en activity-order findings worden in de
  Data-healthprojectie via `validate_transaction_stream()` gecategoriseerd,
  waarbij meerdere findings op één activiteit behouden blijven.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen na de activity-convergentie.
- `uv run pytest -q tests/test_data_health.py -k cash_reconciliation`
  → 2 passed; cashreconciliatie rapporteert nu ook ontbrekende
  balance-snapshots als niet-blokkerende `insufficient_evidence` met valuta en
  stabiele accountcontext, zonder een kunstmatige afwijking te berekenen.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && git diff --check`
  → groen na de cash-evidence uitbreiding.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 228 passed, 3 bestaande warnings; de nieuwe trade/security-,
  valuta- en fee/tax-contracten blijven groen naast destination parity en de
  bestaande Wealthfolio-exporttests.
- `uv run ruff check src/finance_sync/services/wealthfolio_preflight.py src/finance_sync/services/data_health.py tests/test_wealthfolio_preflight.py tests/test_data_health.py && git diff --check`
  → groen na de uitbreiding van de activity-semantiek.
- `uv run pytest -q tests/test_data_health.py -k 'canonical_data_health'`
  → 3 passed; canonical Data health projecteert nu gedeelde security- en
  valutacontract-findings met stabiele transaction IDs naast de bestaande
  activity-order-check.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && git diff --check`
  → groen na de canonical activity-contractprojectie.
- `uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py`
  → 49 passed; ontbrekende stabiele activity-ID’s, security/valuta-contracten
  en gecombineerde findings blijven tenant-scoped en deduplicated zichtbaar.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_wealthfolio_preflight.py tests/test_data_health.py && git diff --check`
  → groen na de idempotency-contract uitbreiding.
- `uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py`
  → 50 passed; cross-currency FX-dekking, stabiele activity-ID’s en de
  canonical contractprojectie blijven groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_data_health.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen na de FX-contract uitbreiding.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 232 passed, 3 bestaande warnings; FX-dekking, canonical Data health,
  destination parity, GUI, preflight en exporter blijven groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_data_health.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k data_health_sync_integrity_is_tenant_scoped -vv`
  → 1 passed; de nieuwe PostgreSQL JSONB-count voor partial-sync is
  tenant-scoped en rapporteert geen runs van een andere tenant.
- `uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py`
  → 53 passed; de JSONB-fix wijzigt de bestaande unitcontracten niet.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 234 passed, 3 warnings in 23.75s; de volledige gerichte Data health,
  destination parity, GUI, preflight en exporter-regressiesuite blijft groen
  na de PostgreSQL-fix.
- `make test-integration`
  → 190 passed, 3867 deselected, 22 warnings in 159s; volledige PostgreSQL/
  Redis integration-suite groen. De eerste run had 1 fout door het gebruik
  van `json_array_length(jsonb)`; dit is gecorrigeerd naar
  `jsonb_array_length()` en opnieuw volledig gevalideerd.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py tests/integration/test_control_plane_phase4_pg.py && git diff --check`
  → groen na de JSONB-count fix.
- `uv run pytest -q tests/test_data_health.py -k 'canonical_data_health'`
  → 4 passed; herhaalde canonical activity-rows leveren één deterministische
  finding op en gecombineerde validator-categorieën blijven afzonderlijk.
- `uv run pytest -q tests/test_data_health.py -k 'portfolio_quantity or tax_lot'`
  → 7 passed; quantity- en tax-lot-evidence bewaart volledige counts naast
  maximaal 100 detailrecords.
- `uv run pytest -q tests/test_data_health.py -k 'canonical_data_health or portfolio_quantity or tax_lot'`
  → 11 passed; zero-cost/order-counts, quantity- en tax-lot-projecties blijven
  groen.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -vv`
  → 7 passed in 2.70s; de nieuwe filtered window-counts werken tegen de echte
  PostgreSQL control-plane/Data health-keten.
- `uv run pytest -q tests/test_data_health.py -k 'canonical_data_health'`
  → 6 passed; incomplete en invalid activity-contract evidence gebruikt de
  window-counts en blijft backwards-compatible voor lightweight query-rows.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && uv run pyright src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py src/finance_sync/api/v1/destinations.py src/finance_sync/observability/destination_metrics.py && git diff --check`
  → Ruff, feature-scoped pyright (**0 errors, 0 warnings**) en diff-check
  groen na de invalid-contract aggregate-uitbreiding.
- `uv run pyright src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py src/finance_sync/api/v1/destinations.py src/finance_sync/observability/destination_metrics.py`
  → 0 errors, 0 warnings na uitbreiding met fee/tax- en cashactiviteiten.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 237 passed, 3 bestaande warnings in 23.67s; de volledige gerichte
  regressiesuite blijft groen na uitbreiding naar alle activity-types.
- `make test-integration`
  → 190 passed, 3870 deselected, 22 warnings in 161.35s; de volledige
  PostgreSQL/Redis integration-suite blijft groen na de fee/tax/cash-
  uitbreiding en filtered contract aggregates.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'data_health or control_plane_data_quality'`
  → 7 passed in 2.66s; de extra contract-count query werkt tenant-scoped in
  PostgreSQL.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'data_health or control_plane_data_quality'`
  → 7 passed in 2.57s; de afzonderlijke invalid-contract window aggregate
  compileert en draait correct tegen PostgreSQL.
- `uv run pytest -q tests/test_data_health.py -k 'canonical_data_health'`
  → 6 passed; fee/tax- en cash-contracts worden canonical geprojecteerd met
  dezelfde blocking/evidence-semantiek.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'data_health or control_plane_data_quality'`
  → 7 passed in 2.63s; de uitgebreide activity-types en aggregate-counts
  blijven tenant-scoped en PostgreSQL-compatibel.
- `uv run pytest -q tests/test_data_health.py -k wealthfolio_preflight`
  → 2 passed; ontbrekende en niet-geverifieerde cost basis worden afzonderlijk
  en als niet-blokkerende warnings geprojecteerd.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'data_health or control_plane_data_quality'`
  → 7 passed in 2.68s; de extra cost-basis query blijft tenant-scoped in
  PostgreSQL.
- `uv run pytest -q tests/test_data_health.py -k cash_reconciliation`
  → 3 passed; snapshot-mismatch, ontbrekende snapshot en transaction-flow-
  mismatch hebben ieder een expliciet bewijsniveau.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'cash_and_quantity or data_health_sync_integrity'`
  → 2 passed in 1.46s; de transaction-flow query blijft tenant-scoped en
  PostgreSQL-compatibel.
- `uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py`
  → 54 passed; canonical Data health en de gedeelde Wealthfolio-validator
  blijven groen na de deduplicatie.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && git diff --check`
  → groen na de deterministische finding-projectie.
- `uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py`
  → 54 passed; bounded evidence voor directe aggregate issues veroorzaakt
  geen regressie in canonical Data health of Wealthfolio preflight.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && git diff --check`
  → groen na de aggregate evidence-uitbreiding.
- `uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_destinations.py tests/test_destination_metrics.py`
  → 78 passed, 1 bestaande warning; canonical deduplicatie, parity-account
  typing en destination metrics blijven groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 235 passed, 3 bestaande warnings in 24.86s; de volledige gerichte
  Data health, destination parity, GUI, preflight en exporter-regressiesuite
  blijft groen na de typecontract-wijzigingen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/api/v1/destinations.py src/finance_sync/observability/destination_metrics.py tests/test_data_health.py tests/test_destinations.py tests/test_destination_metrics.py && git diff --check`
  → groen.
- `uv run pyright src/finance_sync/api/v1/destinations.py src/finance_sync/observability/destination_metrics.py src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py`
  → 0 errors, 0 warnings; de feature-gerelateerde destination, Data health
  en Wealthfolio-preflight contracten zijn typecheck-clean.
- De connector/account-deleteflow is in de bestaande GUI-, OpenAPI- en
  PostgreSQL-integratietests afgedekt met deletion-preview, tenant-scoped
  cascade en audit-event; deze actie blijft expliciet buiten de gewone Data
  health GET-flow.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_destinations_api_pg.py tests/integration/test_control_plane_phase4_pg.py -k 'destination or data_health_sync_integrity'`
  → 9 passed, 5 deselected in 7.25s; typed destination parity en de
  tenant-scoped partial-sync count werken tegen PostgreSQL/Redis.
- `uv run pyright src`
  → 0 errors, 17 warnings; de resterende meldingen zijn bestaande
  niet-blokkerende typewaarschuwingen buiten de feature (connectors,
  enrichment, exporters, control-plane, securities en orchestrator).
- `uv run pyright src`
  → historische tussenmeting: 53 errors en 31 warnings. Deze zijn in latere
  typecontract-commits teruggebracht naar 0 errors en 17 warnings; zie de
  actuele pyright-verificatie hierboven.
- `uv run pytest -q tests/test_data_health.py -k tax_lot`
  → 2 passed; negatieve/gesloten lots en sales boven de beschikbare lotbasis
  worden tenant-scoped als `tax_lot_integrity` geprojecteerd.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && git diff --check`
  → groen na de oversell-validatie.
- `uv run pytest -q tests/test_data_health.py -k tax_lot`
  → 3 passed; oversells blokkeren en opening-lots zonder aankoopbasis geven
  een warning met expliciet bewijsniveau.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && git diff --check`
  → groen na de tax-lot evidence-gap uitbreiding.
- `uv run pytest -q tests/test_data_health.py -k canonical_data_health`
  → 3 passed; incomplete-trade impact count en de detail-limiet worden apart
  en backwards-compatible geprojecteerd.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && git diff --check`
  → groen na de total-count evidence-uitbreiding.
- `uv run pytest -q tests/test_data_health.py -k canonical_data_health`
  → 3 passed; incomplete trades, holdings en securities rapporteren volledig
  impactaantal naast de begrensde detailrecords.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && git diff --check`
  → groen na de holdings/security total-count uitbreiding.
- `uv run pytest -q tests/test_data_health.py -k 'wealthfolio_preflight or canonical_data_health'`
  → 4 passed; valuation- en cost-basisimpact rapporteert nu volledige counts
  met behoud van de bestaande detailprojectie.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && git diff --check`
  → groen na de valuation/cost-basis total-count uitbreiding.
- `uv run pytest -q tests/test_data_health.py -k sync_integrity`
  → 1 passed; partial-sync impact count komt uit de volledige tenant-scoped
  count-query en blijft gescheiden van de 100 detailrecords.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && git diff --check`
  → groen na de partial-sync total-count uitbreiding.
- `uv run pytest -q tests/test_data_health.py -k 'additional_health or sync_integrity'`
  → 2 passed; import- en partial-sync evidence behouden per-run acties en
  tonen bounded aggregate context.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && git diff --check`
  → groen na de import-run evidence-uitbreiding.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 234 passed, 3 bestaande warnings; import-run counts, partial-sync,
  valuation evidence, parity, GUI, preflight en exporter blijven groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_data_health.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 234 passed, 3 bestaande warnings; partial-sync total-count, bounded
  valuation/cost-basis evidence, parity, GUI, preflight en exporter blijven
  groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_data_health.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 234 passed, 3 bestaande warnings; bounded valuation/cost-basis evidence,
  destination parity, GUI, Data health, preflight en exporter blijven groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_data_health.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 234 passed, 3 bestaande warnings; bounded valuation/cost-basis evidence,
  tax-lots, parity, GUI, preflight en exporter blijven groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_data_health.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 234 passed, 3 bestaande warnings; bounded canonical evidence, tax-lots,
  parity, GUI, preflight en exporter blijven groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_data_health.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 234 passed, 3 bestaande warnings; total-count evidence, tax-lots,
  canonical Data health, destination parity, preflight en exporter blijven
  groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_data_health.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 234 passed, 3 bestaande warnings; tax-lot evidence levels, oversell-
  blocking, destination parity, GUI, preflight en exporter blijven groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_data_health.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen.
- `uv run pytest -q tests/test_data_health.py -k 'security_identity or tax_lot or canonical_data_health'`
  → 7 passed; securityreferenties, tax-lot oversells en canonical activity-
  projecties blijven groen na de tenant-scope audit.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && git diff --check`
  → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 233 passed, 3 bestaande warnings; tax-lot, tenant-safe security-
  references, FX-contracten, parity en exporter blijven groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_data_health.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 233 passed, 3 bestaande warnings; tax-lot oversell-detectie blijft groen
  naast destination parity, GUI, Data health, preflight en exporter.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_data_health.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen.
- `uv run pytest -q tests/test_wealthfolio_preflight.py tests/test_data_health.py`
  → 50 passed; lightweight transferparen en ongebalanceerde query-rows volgen
  dezelfde pairingregels zonder regressie in canonical Data health.
- `uv run ruff check src/finance_sync/services/wealthfolio_preflight.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen na de transfer-adapter regressiefix.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 231 passed, 3 bestaande warnings; destination parity, GUI, canonical
  Data health, transfer pairing, preflight en exporter blijven groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_data_health.py tests/test_wealthfolio_preflight.py && git diff --check`
  → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 230 passed, 3 bestaande warnings; de volledige gerichte parity-, GUI-,
  Data health-, preflight- en exporter-suite blijft groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/services/wealthfolio_preflight.py tests/test_wealthfolio_preflight.py tests/test_data_health.py && git diff --check`
  → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_exporter.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 229 passed, 3 bestaande warnings; canonical activity-contracten blijven
  groen samen met destination parity, GUI, preflight en exporter-regressies.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py tests/test_destination_metrics.py`
  → 162 passed, 2 bestaande warnings; destination parity, GUI, Data health,
  Wealthfolio-preflight, connector-contracten en metrics blijven groen na de
  activity- en cash-uitbreidingen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → 155 passed; per-account paritycontract, destination wizard en bestaande
  Data health/preflight/UI-contracts blijven groen.
- `uv run ruff check src/finance_sync/api/v1/destinations.py src/finance_sync/services/data_health.py tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → groen; `git diff --check` → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → 155 passed; typed parity-contracten, wizard-rendering en bestaande
  destination/Data health/preflight/UI-contracts blijven groen.
- `uv run ruff check src/finance_sync/api/v1/destinations.py tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → groen; `git diff --check` → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → 155 passed; per-account parity-output en wizard-rendering blijven
  privacyveilig en backwards-compatible.
- `uv run ruff check src/finance_sync/api/v1/destinations.py src/finance_sync/services/data_health.py tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → groen; `git diff --check` → groen.
- `uv run pytest -q tests/test_data_health.py -k destination`
  → 5 passed; mappings met dezelfde provider-ID zijn alleen conflicterend
  binnen hetzelfde `target_id`.
- `uv run pytest -q tests/test_data_health.py -k 'canonical_data_health or destination'`
  → 6 passed; zero-cost trade-classificatie gebruikt de gedeelde validator
  zonder regressie in de canonical Data health-projectie.
- `uv run pytest -q tests/test_data_health.py -k 'canonical_data_health'`
  → 2 passed; de invalid activity-ordering wordt als blocking issue
  geprojecteerd.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → 153 passed; destination parity, canonical activity semantics en alle
  bestaande Data health/preflight/UI-contracts blijven groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/api/v1/destinations.py tests/test_data_health.py tests/test_destinations.py tests/test_gui_dashboard.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → groen; `git diff --check` → groen.
- `uv run pytest -q tests/test_wealthfolio_preflight.py -k 'destination_probe'`
  → 4 passed; probe-statuscontracten voor ready, unauthorized, unavailable
  en timeout zijn afgedekt.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → 155 passed; destination parity, activity semantics, probe-statussen en
  bestaande Data health/preflight/UI-contracts blijven groen.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/api/v1/destinations.py tests/test_data_health.py tests/test_destinations.py tests/test_gui_dashboard.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → groen; `git diff --check` → groen.
- `uv run pytest -q tests/test_destinations.py tests/test_gui_dashboard.py tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → 150 passed; provider-account identity-normalisatie en alle bestaande
  destination/data-health/preflight/UI-contracts blijven groen.
- `uv run ruff check src/finance_sync/api/v1/destinations.py tests/test_destinations.py tests/test_data_health.py tests/test_gui_dashboard.py tests/test_wealthfolio_preflight.py tests/test_openapi_connectors.py`
  → groen; `git diff --check` → groen.
- `uv run pytest -q tests/test_data_health.py -k security_identity`
  → 2 passed; coverage voor ticker/ISIN-conflict en malformed ISIN.
- `uv run pytest -q tests/test_data_health.py -k portfolio_quantity`
  → 4 passed; coverage voor purchase/sale met percentage- en timestamp-evidence,
  inbound/outbound transfers,
  expliciete split-ratio en niet-modelleerbare corporate action.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py`
  → groen; `git diff --check` → groen.
- `uv run pyright src/finance_sync/services/data_health.py`
  → 0 errors, 0 warnings; percentage- en timestamp-evidence is type-safe.
- `uv run pytest -q tests/test_wealthfolio_preflight.py tests/test_data_health.py`
  → 63 passed; de canonical quantity-event contract, persisted metadata-
  envelope en Data health/preflight-regressies blijven groen.
- `uv run pytest -q tests/test_data_health.py -k 'canonical_projection_matches_shared_preflight_validator or canonical_data_health_projects_fee_and_cash_contracts'`
  → 2 passed; canonical Data health projecteert dezelfde fee-semantiek als
  de shared Wealthfolio preflight-validator.
- `uv run pytest -q tests/test_data_health.py -k 'transaction_relationship or sync_integrity or changed_provider'`
  → 3 passed; relationship-, cursor- en provider-revision-evidence bewaren
  nu counts los van de detail-limiet.
- `uv run pytest -q tests/test_data_health.py -k 'transaction_identity or transaction_fingerprint or transaction_semantic_duplicate or transaction_relationship or sync_integrity or changed_provider'`
  → 6 passed; duplicate-groepen en relationship/sync-integrity evidence
  voldoen aan het count-contract.
- `uv run pytest -q tests/test_data_health.py -k 'cash_reconciliation or canonical_data_health or transaction_identity or transaction_relationship or sync_integrity'`
  → 12 passed; cash-, canonical- en identity/sync-findings behouden de
  uniforme count/detail-limiet evidence.
- `uv run pyright src/finance_sync/services/wealthfolio_preflight.py src/finance_sync/services/data_health.py`
  → 0 errors, 0 warnings.
- `uv run pytest -q tests/test_tax_lots.py`
  → 26 passed, 8 bestaande AsyncMock/resource warnings; split-event
  cost-basisbehoud en ontbrekende-ratio evidence zijn afgedekt.
- `uv run pytest -q tests/test_tax_lots.py -k compute_all_tax_lots`
  → 1 passed; directe reconstructie wist eerst alleen de eigen tenant-scoped
  lotgeneratie.
- `uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_tax_lots.py tests/connectors/degiro_pension/test_degiro_pension_connector.py`
  → 109 passed, 8 bestaande AsyncMock/resource warnings; de gecombineerde
  Data health, Wealthfolio, tax-lot en DeGiro-slice blijft groen.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'cash_and_quantity or data_health_sync_integrity'`
  → 2 passed, 5 deselected; PostgreSQL/Redis cash-, quantity- en sync-
  integriteitsregressies blijven groen.
- `uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_tax_lots.py`
  → 88 passed, 8 bestaande AsyncMock/resource warnings.
- `uv run ruff check src/finance_sync/services/tax_lot_service.py src/finance_sync/api/v1/tax_lots.py tests/test_tax_lots.py`
  → groen; `git diff --check` → groen.
- `uv run pyright src/finance_sync/services/tax_lot_service.py src/finance_sync/api/v1/tax_lots.py`
  → 0 errors, 0 warnings; compute-responsevelden voor quantity-events zijn
  type-safe.
- `uv run pytest -q tests/test_wealthfolio_preflight.py -k quantity_event_ratio tests/test_tax_lots.py -k process_split`
  → 2 passed; geneste eventvelden en `new/old quantity`-equivalenten worden
  veilig genormaliseerd en naar tax-lot verwerking doorgegeven.
- `uv run pytest -q tests/connectors/degiro_pension/test_degiro_pension_connector.py`
  → 20 passed; DeGiro corporate-actionbeschrijvingen worden als canonical
  `corporate_action` geclassificeerd, expliciete ratio's worden opgeslagen
  en fee-classificatie blijft intact.
- `uv run pytest -q tests/connectors/test_saxo_investor.py`
  → 12 passed; Saxo `Corporate action` wordt vóór dividendclassificatie als
  canonical `corporate_action` behouden en expliciete ratio-labels worden in
  hetzelfde `ProviderMetadata.fields.split_ratio`-contract opgeslagen.
- `uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_tax_lots.py -k 'corporate_action or quantity_event'`
  → 8 passed, 85 deselected, 1 bestaande warning; de Saxo-normalisatie sluit
  aan op de bestaande quantity-event, Data health en tax-lot-keten.
- `uv run ruff check src/finance_sync/connectors/saxo_investor.py tests/connectors/test_saxo_investor.py`
  → groen; `uv run pyright src/finance_sync/connectors/saxo_investor.py` → 0
  errors, 0 warnings; `git diff --check` → groen.
- `uv run pytest -q tests/connectors/trading212/test_trading212_connector.py`
  → 76 passed, 1 skipped, zonder de eerdere asyncio-markering-warning; Trading212
  split/corporate-action mapping blijft naast de bestaande connectorflow groen.
- `uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_tax_lots.py tests/connectors/degiro_pension/test_degiro_pension_connector.py tests/connectors/trading212/test_trading212_connector.py`
  → 185 passed, 1 skipped, 8 bestaande AsyncMock/resource warnings; de
  volledige lokale feature-gate blijft groen.
- `uv run pytest -q tests/connectors/trading212/test_trading212_connector.py -k 'parse_split or map_transaction_types'`
  → 4 passed; parsed split-events behouden hun expliciete ratio in het
  canonical metadata-contract.
- `uv run pytest -q tests/connectors/trading212/test_trading212_connector.py -k 'parse_split'`
  → 2 passed; zowel `newQuantity`/`oldQuantity` als `newUnits`/`oldUnits`
  worden tijdens parsing naar hetzelfde ratio-contract genormaliseerd.
- `uv run pytest -q tests/test_data_health.py -k 'canonical_projection'`
  → 2 passed; corporate-action-events worden in de canonical projectie met
  persisted provider-metadata aan dezelfde shared validator aangeboden.
- `uv run pytest -q tests/test_data_health.py`
  → 43 passed; de nieuwe canonical quantity-event parity-regressie blijft
  naast alle bestaande Data health-projecties groen.
- `uv run pytest -q tests/test_data_health.py -k 'portfolio_quantity'`
  → 5 passed; een ratio-event zonder opening quantity-basis blijft evidence
  gap en wordt niet als blocking mismatch geïnterpreteerd.
- `uv run pytest -q tests/test_data_health.py`
  → 44 passed; de quantity-reconciliatie blijft volledig groen na de
  false-positive correctie.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py`
  → groen; `uv run pyright src/finance_sync/services/data_health.py` → 0
  errors, 0 warnings; `git diff --check` → groen.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'data_health'`
  → 6 passed, 1 deselected; de nieuwe account-, destination- en tombstone-
  projecties blijven tenant-scoped tegen PostgreSQL/Redis werken.
- `uv run pytest -q tests/test_data_health.py -k 'account_metadata_identity'`
  → 2 passed; account identity matching herkent zowel bestaande IBAN-velden
  als provider-equivalenten zoals `accountId`/`account_id`, ook wanneer de
  twee accounts verschillende schrijfwijzen gebruiken, tenant-scoped en
  secretvrij; dubbele aliases op één account worden niet dubbel geteld.
- `uv run pytest -q tests/test_data_health.py -k 'selected_account or account_metadata_identity'`
  → 3 passed; ontbrekende `selected_accounts` worden connection-scoped en
  secretvrij als actionable account-identity finding geprojecteerd.
- `uv run pytest -q tests/test_data_health.py -k 'orphaned_account or selected_account or account_metadata_identity'`
  → 4 passed; ontbrekende connectorrelaties, geselecteerde accounts en
  metadata-aliases blijven tenant-scoped en deterministisch.
- `uv run pytest -q tests/test_data_health.py`
  → 47 passed; de nieuwe account-relatiecontroles veroorzaken geen regressie
  in de bestaande Data health-projecties.
- `uv run pytest -q tests/test_data_health.py -k 'orphaned_account'`
  → 1 passed; een actieve account zonder bestaande connector wordt als
  niet-blokkerende account-identity finding met repairvrije GUI-navigatie
  geprojecteerd.
- `uv run pytest -q tests/test_data_health.py -k 'tombstoned_export or destination_parity'`
  → 3 passed; lokaal getombstonede transacties binnen een delivery-scope
  worden als warning met expliciete remote-verificatiebehoefte geprojecteerd,
  ook wanneer de tombstone ná de laatste delivery is aangemaakt.
- `uv run pytest -q tests/test_data_health.py`
  → 48 passed; de tombstone/export-scopecontrole veroorzaakt geen regressie
  in de bestaande Data health-projecties.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py`
  → groen; `uv run pyright src/finance_sync/services/data_health.py` → 0
  errors, 0 warnings; `git diff --check` → groen.
- `uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py`
  → 65 passed; Data health en de onafhankelijke Wealthfolio preflight delen
  dezelfde quantity-event semantiek.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'data_health'`
  → 6 passed, 1 deselected; de gewijzigde canonical query werkt ook tegen
  PostgreSQL/Redis en behoudt de tenant-scoped Data health-integriteit.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'account_selection_orphan_and_tombstone'`
  → 1 passed, 7 deselected; de nieuwe selectie-, orphan- en tombstonechecks
  zijn tegen echte PostgreSQL-records gevalideerd.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'data_health'`
  → 7 passed, 1 deselected; de gecombineerde Data Health-integratieset blijft
  groen nadat deze regressietest is toegevoegd.
- `uv run pytest -q tests/test_data_health.py -k 'sync_integrity'`
  → 2 passed; orphaned cursors, gedeeltelijke runs en cursors die achterlopen
  op de laatste succesvolle connection-run worden afzonderlijk geprojecteerd.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'account_selection_orphan_and_tombstone'`
  → 1 passed, 7 deselected; dezelfde stale-cursorcontrole is met echte
  PostgreSQL `SyncCursor`- en `SyncRun`-records gevalideerd.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'account_selection_orphan_and_tombstone'`
  → 1 passed, 7 deselected; de geselecteerde account die in het laatste
  runrapport ontbreekt wordt naast de stale cursor tegen PostgreSQL gevonden.
- `uv run pytest -q tests/test_data_health.py`
  → 49 passed; de cursorcontrole veroorzaakt geen regressie in Data health.
- `uv run pytest -q tests/test_data_health.py`
  → 50 passed; de selected-account-gapprojectie en de bestaande
  sync-integriteitschecks blijven samen groen.
- `uv run pytest -q tests/test_data_health.py tests/test_sync_orchestrator.py tests/test_enums.py`
  → 127 passed, 76 bestaande warnings; de nieuwe run-reportvelden zijn
  backwards-compatible met de sync-orchestrator en bestaande enumcontracten.
- `uv run pytest -q tests/test_sync_orchestrator.py -k 'full_pipeline'`
  → 1 passed, 64 deselected, 4 bestaande AsyncMock/resource warnings; de
  succesvolle pipeline schrijft de verwachte `account_external_ids` naar het
  runrapport.
- `make test-integration`
  → 191 passed, 3905 deselected, 22 bestaande warnings in 163.87s; de volledige
  PostgreSQL/Redis-suite blijft groen inclusief migraties, connector-API’s,
  Trading212-sync, upserts, destination parity en de nieuwe Data Health-
  regressies.
- `uv run pytest -q tests/connectors/test_saxo_investor.py -k 'corporate_action_ratio'`
  → 4 passed, 9 deselected; Saxo-ratio’s in `2:1`, `for`, `op` en slash-notatie
  worden veilig naar hetzelfde canonical ratio-contract genormaliseerd.
- `uv run ruff check src/finance_sync/connectors/saxo_investor.py tests/connectors/test_saxo_investor.py && uv run pyright src/finance_sync/connectors/saxo_investor.py && git diff --check`
  → alle checks groen; 0 pyright-errors en 0 pyright-warnings voor de Saxo-
  wijziging.
- `uv run pytest -q tests/test_data_health.py -k 'security_identity'`
  → 5 passed; malformed ISIN’s, niet-ISO-valutacodes, cross-variant
  tickerambiguïteit en listing-venue-evidence worden als passende
  security-integrity findings geprojecteerd.
- `uv run pytest -q tests/test_data_health.py`
  → 52 passed; de strengere currency- en ticker-variantvalidatie veroorzaakt
  geen regressie in de overige Data Health-projecties.
- `APP_ENVIRONMENT=dev DEBUG=false uv run pytest -q tests/test_data_health.py tests/test_control_plane_api.py tests/test_gui_dashboard.py tests/test_openapi_connectors.py`
  → 160 passed, 1 bestaande warning; Data health, API, GUI en OpenAPI-
  contracten blijven backwards-compatible na listing-aware security evidence.
- `uv run pytest -q tests/test_data_health.py -k 'account_metadata_identity'`
  → 3 passed; account-id-aliases voor monetary-, bank- en portfolio-identiteit
  worden provider-neutraal en secretvrij gegroepeerd.
- `uv run pytest -q tests/test_data_health.py`
  → 53 passed; de uitgebreidere metadata-allowlist veroorzaakt geen regressie.
- `uv run pytest -q tests/test_data_health.py`
  → 55 passed; de cost-basis-per-unit-consistentie veroorzaakt geen regressie
  in de overige Data Health-projecties.
- `uv run pytest -q tests/test_data_health.py -k 'tax_lot'`
  → 6 passed; quantity-, relatie-, oversell-, cost-basis-, per-unit- en
  ontbrekende-lotbasisgevallen worden afzonderlijk als blocking of warning
  geprojecteerd.
- `uv run pytest -q tests/test_data_health.py -k 'tax_lot'`
  → 6 passed; cost-basisfindings bevatten naast de aggregate count ook
  reason-coded lotdetails voor de GUI.
- `uv run pytest -q tests/test_data_health.py`
  → 56 passed; unbacked-sale evidence veroorzaakt geen regressie in de
  overige Data Health-projecties.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py && uv run pyright src/finance_sync/services/data_health.py && git diff --check`
  → alle checks groen; de tax-lot cost-basisuitbreiding introduceert geen
  lint-, type- of whitespace-regressie.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'tax_lot_cost_basis'`
  → 1 passed, 9 deselected; een ongeldig cost-basisveld en een verkoop zonder
  lotbasis worden uitsluitend in de owning tenant geprojecteerd; de eerste
  blijft blocking en de tweede wordt als evidence gap gerapporteerd.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'metadata_identity'`
  → 1 passed, 8 deselected; provider-aliasen blijven tenant-scoped tegen
  PostgreSQL en lekken geen account uit een andere tenant.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'data_health'`
  → 7 passed, 1 deselected; de connection-scoped `MAX(cursor)`-referentie
  werkt tegen PostgreSQL zonder afhankelijkheid van de 100-run detailwindow.
- `uv run pytest -q tests/test_data_health.py`
  → 56 passed; de volledige lokale Data Health-projectie blijft groen na de
  unbacked-sale en cost-basisconsistentie-uitbreidingen.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'data_health'`
  → 9 passed, 1 deselected; alle PostgreSQL Data Health-integriteits- en
  tenant-isolatieprojecties blijven groen.
- `uv run ruff check src/finance_sync/services/data_health.py tests/test_data_health.py`
  → groen; `uv run pyright src/finance_sync/services/data_health.py` → 0
  errors, 0 warnings; `git diff --check` → groen.
- `uv run pytest -q tests/test_wealthfolio_preflight.py tests/test_data_health.py`
  → 67 passed; transfer-metadata envelopes en ontbrekende quantities van
  security-bearing transfers worden door de shared validator afgedekt.
- `uv run pytest -q tests/test_wealthfolio_exporter.py tests/test_wealthfolio_preflight.py tests/test_wealthfolio_network_privacy.py`
  → 91 passed, 1 bestaande Sentry-deprecation warning; de exporter gebruikt
  dezelfde transferregels zonder regressie in netwerk/privacygedrag.
- `uv run ruff check src/finance_sync/services/wealthfolio_preflight.py tests/test_wealthfolio_preflight.py`
  → groen; `uv run pyright src/finance_sync/services/wealthfolio_preflight.py`
  → 0 errors, 0 warnings; `git diff --check` → groen.
- `uv run pytest -q tests/test_enums.py tests/test_coverage_gaps.py -k 'transaction_persistence'`
  → 2 passed; persistence behoudt de nieuwe `corporate_action`-enumwaarde
  en blijft onbekende typen naar `other` normaliseren.
- `uv run pytest -q tests/test_wealthfolio_exporter.py -k 'corporate_action'`
  → 1 passed; corporate actions worden downstream als Wealthfolio
  `ADJUSTMENT` met subtype `CORPORATE_ACTION` geprojecteerd.
- `uv run pytest -q tests/test_enums.py tests/test_wealthfolio_exporter.py tests/test_sync_upserts.py`
  → 97 passed, 1 bestaande Sentry-deprecation warning; enum-, mapper- en
  persistence-regressies blijven samen groen.
- `uv run ruff check src/finance_sync/models/enums.py src/finance_sync/connectors/models.py src/finance_sync/exporter/wealthfolio/transaction_mapper.py tests/test_enums.py tests/test_coverage_gaps.py tests/test_wealthfolio_exporter.py`
  → groen; `uv run pyright src/finance_sync/models/enums.py src/finance_sync/connectors/models.py src/finance_sync/exporter/wealthfolio/transaction_mapper.py`
  → 0 errors, 0 warnings; `git diff --check` → groen.
- `uv run pytest -q tests/test_data_health.py tests/test_wealthfolio_preflight.py tests/test_tax_lots.py tests/test_enums.py tests/test_sync_upserts.py tests/test_wealthfolio_exporter.py tests/connectors/degiro_pension/test_degiro_pension_connector.py tests/connectors/trading212/test_trading212_connector.py`
  → 286 passed, 1 skipped, 9 bestaande warnings; de actuele canonical
  corporate-action-, quantity-, tax-lot-, connector- en Wealthfolio-keten
  blijft lokaal groen.
- `make test-integration`
  → 190 passed, 3895 deselected, 22 bestaande warnings in 163.95s; de
  PostgreSQL/Redis integration-suite blijft groen na de enum/persistence-
  en Wealthfolio-mappingwijziging.
- `uv run pytest -q tests/test_wealthfolio_preflight.py tests/test_data_health.py -k quantity_event_ratio`
  → 5 passed; flat, envelope, geneste en `newUnits`/`oldUnits`-ratio's blijven
  provider-neutraal gevalideerd.
- `uv run pytest -q tests/test_data_health.py`
  → 42 passed; samengestelde overview-volgorde en alle Data health-projecties
  blijven groen.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'data_health'`
  → 6 passed, 1 deselected; de deterministische overview-sortering blijft
  tenant-scoped en API-compatibel tegen PostgreSQL/Redis.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_trading212_sync_pipeline_pg.py`
  → 13 passed, 8 bestaande Sentry-deprecation warnings; selectie, rollback,
  account-idempotentie en duplicate-preventie zijn tegen PostgreSQL/Redis
  bevestigd.
- `make test-integration`
  → 190 passed, 3889 deselected, 22 bestaande warnings in 159.94s; de volledige
  PostgreSQL/Redis integration-suite is groen inclusief tenant-isolatie,
  migration roundtrips, destination parity, Trading212 sync en upsert-
  idempotentie.
- `uv run ruff check src/finance_sync/connectors/trading212.py tests/connectors/trading212/test_trading212_connector.py`
  → groen; `git diff --check` → groen.
- `uv run pyright src/finance_sync/connectors/trading212.py`
  → 0 errors, 0 warnings.
- `uv run ruff check src/finance_sync/connectors/degiro_pension.py tests/connectors/degiro_pension/test_degiro_pension_connector.py`
  → groen; `git diff --check` → groen.
- `uv run pyright src/finance_sync/connectors/degiro_pension.py`
  → 0 errors, 0 warnings; nullable mutation-bedragen worden expliciet
  genarrowed vóór FX-reconciliatie.
- `uv run ruff check src/finance_sync/services/tax_lot_service.py tests/test_tax_lots.py`
  → groen; `git diff --check` → groen.
- `uv run pyright src/finance_sync/services/tax_lot_service.py`
  → 0 errors, 0 warnings.
- `uv run pytest -q tests/test_tax_lots.py tests/test_data_health.py tests/test_wealthfolio_preflight.py`
  → 127 passed, 8 bestaande warnings; pairing, idempotente transfer-basis-
  migratie, tax-lotintegriteit en bestaande Data Health/Wealthfolio-contracten
  blijven groen.
- `uv run ruff check src/finance_sync/services/tax_lot_service.py src/finance_sync/services/data_health.py src/finance_sync/models/tax_lot.py src/finance_sync/db/repositories.py tests/test_tax_lots.py migrations/versions/0065_tax_lot_transfer_origin.py`
  → groen; gerichte Pyright-run op de gewijzigde bronbestanden → 0 errors,
  0 warnings.
- `make test-integration`
  → 193 passed, 3937 deselected, 22 bestaande warnings in 161.37s; de nieuwe
  migration `0065` doorloopt upgrade/downgrade/head-roundtrips en de volledige
  PostgreSQL/Redis integration-suite blijft groen.
- `uv run pytest -q tests/test_tax_lots.py -k 'process_split'`
  → 2 passed; quantity, remaining quantity, cost-basis-per-unit en totale
  cost basis zijn expliciet gevalideerd.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'cash_and_quantity or data_health_sync_integrity'`
  → 2 passed, 5 deselected; PostgreSQL quantity/cash-integriteitsregressies
  blijven groen.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'cash_and_quantity or tax_lot_cost_basis'`
  → 2 passed, 8 deselected; database-backed cash/quantity- en tax-lotchecks
  blijven groen na de transfer-contractwijziging.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'data_health'`
  → 9 passed, 1 deselected; de PostgreSQL/Redis Data-healthprojecties blijven
  groen na de canonical trade-semantic aggregate-uitbreiding.
- `DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/finance_sync_test TEST_REDIS_URL=redis://localhost:6380/15 uv run pytest -q tests/integration/test_control_plane_phase4_pg.py -k 'identity'`
  → 3 passed, 7 deselected; tenant-scoped security/account identity blijft
  groen na toevoeging van listing-aware security evidence.
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/schemas/data_health.py tests/test_data_health.py`
  → groen.
- `uv run pytest -q tests/integration/test_control_plane_phase4_pg.py`
  → testbestand wordt correct verzameld; lokaal 4 tests overgeslagen omdat
  PostgreSQL en Redis niet zijn geconfigureerd (`TEST_DATABASE_URL`/
  `TEST_REDIS_URL` of `make test-integration` vereist).
- `uv run ruff check src/finance_sync/services/data_health.py src/finance_sync/schemas/data_health.py tests/test_data_health.py tests/integration/test_control_plane_phase4_pg.py`
  → groen.
- `uv run pytest -q -rs tests/integration/test_control_plane_phase4_pg.py`
  → 4 tests correct verzameld; lokaal overgeslagen omdat PostgreSQL en Redis
  niet zijn geconfigureerd (`TEST_DATABASE_URL`/`TEST_REDIS_URL` of
  `make test-integration` vereist).
- `git diff --check` → groen.

### Bekende beperking / volgende slice

- De accountcheck signaleert de Trading212 legacy/current-case, maar voert
  bewust geen merge of repair uit.
- Generieke identity matching op provider metadata is nog niet volledig
  provider-onafhankelijk: metadata buiten de allowlist en provider-specifieke
  equivalenten worden nog niet herkend. Semantische duplicates zonder
  bruikbare provider-ID zijn wel geïmplementeerd en tenant-scoped getest.
- Quantity-reconciliatie verwerkt purchase/sale, security-bearing transfers en
  expliciete split/adjustment/corporate-action ratio's.
- Transfers zonder niet-nul amount/direction worden bewust niet in de
  quantity-berekening opgenomen; de transfer-integriteitscheck signaleert ze
  nu expliciet als blocking `incomplete_transaction` voordat downstream
  export/preflight doorgaat.
- De transfer-adapter kan bij lightweight query-rows alleen pairen op de
  beschikbare datum/valuta/absolute hoeveelheid; provider-specifieke transfer-
  identifiers blijven leidend zodra ze in de volledige canonical stream
  aanwezig zijn.
- Corporate actions zonder een positieve ratio worden alleen als
  `insufficient_evidence` gemeld; provider-specifieke event-normalisatie
  buiten het expliciete ratio-contract is nog niet geïmplementeerd. DeGiro en
  Saxo leveren nu bekende tekstuele ratio's aan dat contract. De persisted
  provider-metadata-envelope wordt veilig genormaliseerd en de tax-lot
  reconstructie kan expliciete ratio-events automatisch toepassen. De
  generieke tax-lotintegriteitschecks zijn actief.
- Tax-lot oversell-detectie gebruikt bewust alleen transacties waarvoor in de
  tenant een lotbasis bestaat; openingsposities zonder aankooptransactie geven
  nu een warning maar kunnen niet volledig worden gereconstrueerd.
- Security-bearing transfers worden nu ook in de tax-lot reconstructie
  verwerkt. Niet-pairbare legs, ontbrekende providerreferenties en transfers
  waarvoor geen volledige bronlotbasis bestaat blijven bewust als
  `insufficient_evidence`/transfer-basis-gap zichtbaar; er wordt dan geen
  gedeeltelijke basis verplaatst.
- De gedeelde validator is nu aangesloten op Wealthfolio transaction
  preflight, de quantity-integriteitscheck, de canonical transferprojectie,
  de zero-cost/activity-order projectie, trade/security/fee/tax-semantiek en
  stabiele activity-ID’s in de canonical Data-healthcontractprojectie. De
  incomplete-trade query blijft
  een aparte tenant-scoped detailprojectie voor backwards-compatible
  GUI-evidence; currency/security consistency is nu ook in de canonical
  activity-row beschikbaar wanneer de volledige queryvorm wordt gebruikt.
- Cross-currency trades zonder FX-rate zijn bewust warnings: de onderliggende
  transactie kan legitiem in een andere securityvaluta zijn, maar de export
  moet de ontbrekende conversie-evidence expliciet tonen.
- Security identity matching gebruikt de velden uit `Security` en, wanneer
  aanwezig, aanvullende MIC/venue-evidence uit `SecurityListing`. Een
  tickerconflict blijft bewust een warning en wordt nooit automatisch
  samengevoegd; listingdata bepaalt alleen de uitlegbaarheid van de finding.
- De PostgreSQL tenant-isolationtests en de database-backed remote-
  paritygedragsdekking zijn lokaal uitgevoerd tegen de integration-
  testconfiguratie; de remote probe blijft in deze tests expliciet gemockt.
- Cash reconciliation gebruikt nu de openingssnapshot plus tenant-scoped
  canonical cashactiviteiten wanneer twee bruikbare snapshots beschikbaar
  zijn. Alleen accounts zonder actuele snapshot of betrouwbare openingsbasis
  blijven een niet-blokkerende `insufficient_evidence`-finding houden; er is
  geen automatische cashrepair.
- De identity-, sync-integrity-, cash-, quantity- en metadata-identity-
  scenario's zijn lokaal tegen een echte PostgreSQL/Redis-stack uitgevoerd.
- Destination parity controleert nu de lokale exportledger en mappings. De
  remote probe is aangesloten op de expliciete destination-testactie, met
  feature flag, timeout en rate-limit; Data health GET blijft read-only en
  heeft geen externe netwerkafhankelijkheid.
- De GUI gebruikt hiervoor de bestaande Data health issue/action-projectie;
  aggregate paritycounts zijn nu uitklapbaar zichtbaar op de issuekaart.
  Een volledige per-account remote drill-down blijft bewust buiten scope om
  remote identifiers en payloads niet naar de normale GET-flow te brengen.
- Remote asset/activity parity is read-only; automatische remote create/merge
  of repair blijft bewust buiten scope.
- De nieuwe checks worden via de bestaande Data-health issuekaart en de
  destination-wizard gepresenteerd; er is geen aparte remote-payloadweergave.
- Vervolgwerk is optioneel en valt buiten de afgeronde scope: normalisatie van
  nog niet ondersteunde providerformats, aanvullende cost-basis-events en een
  optionele remote parity-drill-down buiten de read-only aggregateflow.
  Behoud bij volgende uitbreidingen de PostgreSQL-regressie voor geselecteerde
  accounts, orphaned connections en delivery-scope/tombstone-drift; voeg bij
  wijzigingen aan `DataHealthIssue`-velden tests toe op de juiste
  top-level/evidence-projectie. Houd bij verdere sync-reportuitbreidingen de
  backwards-compatibiliteit met oudere runs zonder resource-identiteiten in
  stand.
  De feature-gerelateerde pyright-
  gate is groen; alleen de 17 bestaande projectbrede warnings blijven over.
  De PostgreSQL/Redis integration-suite is voor de huidige slice al opnieuw
  groen uitgevoerd.

## Context

De huidige Data health-flow combineert operationele bronstatus, reconciliatie,
canonical-data checks en Wealthfolio-preflight. De belangrijkste resterende
risico's zitten in identiteit en volledigheid:

- dezelfde provideraccount kan meerdere lokale accounts hebben wanneer
  `external_account_id` of `connection_id` verschilt, bijvoorbeeld een legacy
  Trading212-account met `connection_id = NULL` naast de actuele account;
- transacties kunnen over connection boundaries dubbel voorkomen;
- holdings, transacties, cash en tax lots worden nog niet volledig als één
  consistente portefeuille gecontroleerd;
- Wealthfolio-pariteit wordt vooral tijdens export gecontroleerd, maar niet als
  blijvende Data health-status;
- succesvolle syncs kunnen gedeeltelijk zijn per account/resource zonder dat de
  gebruiker één samengevoegd, actiegericht issue ziet.

De canonical dataset in finance-sync blijft de bron van waarheid. Data health
mag geen brokerdata of Wealthfoliodata zelfstandig wijzigen, behalve via een
expliciet goedgekeurde repair-actie die al binnen de bestaande control-plane
permissions en auditregels valt.

Relevante bestaande onderdelen:

- `src/finance_sync/services/data_health.py`
- `src/finance_sync/services/data_quality.py`
- `src/finance_sync/schemas/data_health.py`
- `src/finance_sync/schemas/data_quality.py`
- `src/finance_sync/services/control_plane.py`
- `src/finance_sync/services/control_plane_actions.py`
- `src/finance_sync/exporter/wealthfolio/`
- `src/finance_sync/models/{account,transaction,holding,tax_lot,security}.py`
- `tests/test_data_health.py`
- `tests/test_data_quality.py`
- `tests/integration/test_control_plane_phase4_pg.py`

## Doel

Maak Data health een betrouwbare, tenant-scoped controlelaag die vóór een
Wealthfolio-export kan vaststellen:

1. of accounts en transacties uniek en correct aan een connector gekoppeld
   zijn;
2. of cash, holdings, securities en tax lots intern consistent zijn;
3. of alle noodzakelijke gegevens voor Wealthfolio aanwezig zijn;
4. of de destination-projectie niet achterloopt of afwijkt;
5. welke herstelactie veilig en concreet beschikbaar is.

## Scope en grenzen

In scope:

- read-only detectie van nieuwe integriteitsproblemen;
- typed API-contracten en stabiele issue-ID's;
- details per connector/account/security/transaction;
- waarschuwingen en blockers voor Wealthfolio-export;
- PostgreSQL- en unit/regressietests;
- beperkte, expliciet bevestigde repair-acties als een bestaande veilige
  service kan worden hergebruikt.

Niet in scope:

- automatisch samenvoegen of verwijderen van financiële data zonder
  expliciete gebruikersbevestiging;
- wijzigingen aan de canonical dataset tijdens een gewone GET van Data health;
- nieuwe Wealthfolio-functionaliteit in Wealthfolio zelf;
- het vervangen van `DataQualityService` of `ControlPlaneService`; composeer
  bestaande projecties waar dat logisch blijft;
- het veranderen van de betekenis van bestaande issue-categorieën zonder
  backwards-compatible contractwijziging.

## Implementatievolgorde

Werk in onderstaande verticale stappen. Iedere stap moet code, tests en een
kort contractcommentaar bevatten voordat de volgende stap start.

### Stap 1 — Baseline en contract uitbreiden

1. Voeg aan `DataHealthCategory` alleen nieuwe, semantisch specifieke waarden
   toe waar bestaande categorieën niet volstaan, bijvoorbeeld:

   - `account_identity_conflict`
   - `duplicate_transaction_identity`
   - `portfolio_quantity_mismatch`
   - `cash_reconciliation_mismatch`
   - `invalid_activity_semantics`
   - `security_identity_conflict`
   - `tax_lot_integrity`
   - `destination_drift`
   - `partial_sync`

2. Breid `DataHealthIssue` uit met optionele, backwards-compatible velden:

   - `connection_id: str | None`
   - `account_ids: list[str]`
   - `security_ids: list[str]`
   - `affected_record_count: int`
   - `blocking: bool`
   - `repair_available: bool`
   - `evidence: dict[str, object]`

3. Houd `details` privacyveilig: geen credentials, API-key, bearer token of
   volledige gevoelige providerpayloads.

4. Maak issue-ID's deterministisch en tenant-onafhankelijk, bijvoorbeeld:

   `account-identity:{provider}:{connection-or-legacy}:{identity-hash}`

   Gebruik geen willekeurige UUID per request, zodat de GUI issues kan volgen.

5. Voeg contracttests toe voor serialisatie, backwards compatibility en
   tenant-isolatie.

Verificatie:

- bestaande `tests/test_data_health.py` en API-contracttests blijven groen;
- OpenAPI bevat de nieuwe optionele responsevelden;
- een tweede GET met dezelfde databasefeiten levert dezelfde issue-ID's.

### Stap 2 — Account identity health

Maak in `DataHealthService` een aparte methode, bijvoorbeeld
`_account_identity_issues()`, die minimaal controleert:

1. dubbele accounts met dezelfde `(tenant_id, provider_key,
   external_account_id)`;
2. dezelfde provideraccount over meerdere `connection_id`-waarden;
3. dezelfde provideraccount met één of meer `connection_id IS NULL`-rijen;
4. Trading212 legacy-ID `trading212` naast een numerieke/current ID;
5. verdachte duplicaten op basis van dezelfde provider-metadata/account-id,
   gelijke valuta en vrijwel gelijke actuele saldi;
6. accounts van een connector die niet meer bestaat of niet meer bij de
   actieve credential hoort;
7. `selected_accounts` die verwijzen naar ontbrekende lokale accounts.

Gebruik de echte connector-scoping in alle queries. Groepeer nooit alleen op
naam of saldo; dat zijn signalen, geen identiteit.

Issues moeten details tonen met lokale account-id, external id,
connection-id, naam, valuta en saldo, maar niet met secrets.

Herstelactie:

- initieel alleen `view_accounts`/connectorbeheer;
- geen automatische merge in deze stap;
- voeg eventueel een aparte preview-action toe als er al een veilige
  account-merge service bestaat.

Tests:

- exact duplicate external ID;
- legacy `NULL connection_id` plus actuele connection;
- Trading212 fallback-ID plus actuele ID;
- twee legitieme accounts van twee onafhankelijke connectors;
- cross-tenant records die elkaar nooit mogen detecteren.

### Stap 3 — Transaction identity en sync completeness

Voeg `_transaction_integrity_issues()` toe met:

1. dezelfde provider/external transaction ID over verschillende connections;
2. dezelfde provider fingerprint op meerdere transacties;
3. semantische duplicaten met dezelfde account, datum, type, bedrag, valuta,
   quantity en security wanneer een provider-ID ontbreekt;
4. transacties met ongeldige of ontbrekende connector/account-relatie;
5. transactions met `tombstoned_at` die nog in een actieve exportscope vallen;
6. provider-revisies die niet opnieuw verwerkt zijn;
7. geselecteerde accounts die niet terugkwamen in de laatste sync;
8. cursors zonder actieve connector/account of cursors die ouder zijn dan de
   laatste succesvolle run;
9. runs waarin accounts, holdings of transactions gedeeltelijk verwerkt zijn.

Gebruik eerst database-aggregaties en beperk detailresultaten tot een
configureerbare limiet, bijvoorbeeld 100 records per issue. Toon altijd het
totaalaantal apart van de detail-limiet.

Maak onderscheid tussen:

- echte duplicate blocker;
- mogelijke duplicate ter beoordeling;
- normale revision/update.

Tests moeten idempotentie, verschillende providers, `NULL` connection IDs,
revisions en partial runs afdekken.

### Stap 4 — Canonical portfolio consistency

Voeg controles toe die per account/security de volgende relaties toetsen:

#### Holdings versus activiteiten

Bereken een verwachte quantity uit BUY, SELL, TRANSFER en SPLIT-achtige
activiteiten. Vergelijk die met de laatste actieve holding. Rapporteer:

- absolute afwijking;
- percentage-afwijking;
- laatste holdingdatum;
- laatste activiteitdatum;
- ontbrekende opening/transfer/corporate-action data.

Gebruik decimalen en configureerbare toleranties; gebruik geen floats.

#### Cash reconciliation

Per account en valuta:

```text
opening balance
+ deposits
- withdrawals
+ dividends/interest
- fees/taxes
+ internal/external transfers
= expected current cash
```

Vergelijk dit met de actuele account/balance-snapshot. Maak expliciet bekend
wanneer er geen betrouwbare opening balance is; markeer dat dan als
`insufficient_evidence` in `evidence`, niet automatisch als fout.

#### Tax lots

Detecteer:

- negatieve `remaining_quantity`;
- lots zonder geldige aankooptransactie;
- lots die verwijzen naar niet-bestaande securities/accounts;
- gesloten lots met positieve resterende quantity;
- holding cost basis zonder lots of transactiegrondslag;
- verkopen die meer quantity verbruiken dan beschikbare lots.

De eerste implementatie is read-only en blokkeert export alleen voor
onmiskenbare integriteitsfouten.

Tests:

- correcte BUY/SELL-keten;
- gedeeltelijke verkoop;
- transfer tussen accounts;
- split/corporate action;
- multi-currency cash;
- ontbrekende opening balance;
- negatieve/gebroken tax lot.

### Stap 5 — Activity en security semantics voor Wealthfolio

Breid de bestaande Wealthfolio-preflight uit met één centrale validator die
dezelfde regels gebruikt vóór export en in Data health:

1. BUY/SELL vereist security, quantity en unit price;
2. fee/tax heeft positieve fee/tax-waarde en geldige valuta;
3. cashactiviteiten hebben bedrag en valuta;
4. transferactiviteiten bevatten richting, bron/doel en waar nodig quantity
   en cost basis;
5. `occurred_at` en `booked_at` zijn logisch geordend;
6. currency codes zijn ISO-4217 en consistent met security/account;
7. security-identiteit is stabiel: ISIN/ticker/exchange/quote currency mogen
   niet conflicteren;
8. dezelfde ISIN mag niet naar meerdere onverenigbare security-records wijzen;
9. iedere exporteerbare activiteit heeft een stabiele idempotency key.

Maak per fout duidelijk of deze:

- export blokkeert;
- alleen een waarschuwing geeft;
- door enrichment/repair kan worden opgelost.

Voorkom dat Data health en de exporter verschillende validatieregels hebben:
deel een service onder `services/wealthfolio_preflight.py` of een passende
submodule.

### Stap 6 — Destination parity voor Wealthfolio

Voeg een optionele destination-check toe die alleen draait wanneer een
Wealthfolio-target geconfigureerd en bereikbaar is. De check mag geen
financiële waarden in logs schrijven.

Controleer minimaal:

- finance-sync accounts versus Wealthfolio accounts;
- `providerAccountId`/mapping-consistentie;
- ontbrekende of vreemde remote accounts binnen de ownership boundary;
- canonical activities versus delivered activities/cursors;
- stale activiteiten die lokaal zijn verwijderd of ingetrokken;
- assets en quotes die lokaal bestaan maar niet remote;
- laatste exportstatus en laatste succesvolle delivery per account;
- holdings quantity/value-reconciliatie na export;
- exportcursor die stilstaat ondanks nieuwe canonical records.

Als de destination niet bereikbaar is, rapporteer `unavailable` of
`destination_check_skipped` met duidelijke reden; verander de algemene
canonical health niet stilzwijgend naar gezond.

Voeg een timeout en rate-limit toe. De GET Data health-pagina mag niet
onbeperkt wachten op Wealthfolio.

Tests:

- remote parity volledig;
- remote ontbrekend account;
- extra remote account;
- stale delivery;
- endpoint timeout/401;
- target niet geconfigureerd;
- tenant/target isolation.

### Stap 7 — GUI en herstelworkflow

Pas `src/finance_sync/templates/dashboard.html` aan zodat ieder issue toont:

- severity en blocking-status;
- connector/account/security-context;
- totaal aantal getroffen records;
- beperkte evidence-details;
- duidelijke actieknop;
- dry-run/preview vóór destructieve repair;
- link naar relevante connector-, account-, transaction- of exporter-view.

Voeg geen automatische merge/delete toe aan de GET-flow. Voor repairacties:

1. preview met aantallen en gekozen primary record;
2. expliciete bevestiging;
3. transactionele uitvoering;
4. audit-event met actor, bronrecords en resultaat;
5. Data health opnieuw laden.

De bestaande connector/account-deleteflow mag hiervoor alleen worden gebruikt
als de ownership boundary en cascade-dekking expliciet worden getoond.

### Stap 8 — Performance, privacy en rollout

1. Indexeer alleen na query-plannen te controleren. Denk aan combinaties rond:

   - `(tenant_id, provider_key, external_account_id, connection_id)`;
   - `(tenant_id, connection_id, occurred_at)`;
   - `(tenant_id, account_id, security_id, observed_at)`;
   - fingerprints en external IDs.

2. Gebruik aggregaties en paginated details; laad geen volledige historische
   dataset in Python voor de normale health-pagina.

3. Voeg een `check_version` of `generated_at` toe wanneer meerdere checks
   gefaseerd worden uitgevoerd.

4. Redigeer provider metadata en remote responses vóór logging of GUI-output.

5. Meet duur en aantallen per check via bestaande observability, zonder
   accountbedragen of identifiers in metricslabels.

6. Voeg een feature flag toe voor destination parity als de remote API nog
   niet stabiel genoeg is voor iedere productie-request.

## Acceptatiecriteria

- [x] Data health detecteert een Trading212 legacy-account met
      `connection_id = NULL` naast de actuele account.
- [x] Data health detecteert dezelfde providertransactie over verschillende
      connection IDs.
- [x] Legitieme accounts met verschillende connectors worden niet als
      duplicaat gemarkeerd.
- [x] Alle queries zijn tenant-scoped en connection-aware.
- [x] Account-, holdings-, transaction-, security- en tax-lotissues bevatten
      stabiele IDs, aantallen en privacyveilige evidence.
- [x] Holdings-versus-transaction quantity-afwijkingen worden per
      account/security getoond met decimalentolerantie.
- [x] Cash reconciliation rapporteert valuta, bewijsniveau en afwijking.
- [x] Onvolledige Wealthfolio-activiteiten blokkeren export wanneer dat
      noodzakelijk is en geven anders een warning.
- [x] Security identity collisions en ontbrekende quote/FX-dekking zijn
      zichtbaar vóór export.
- [x] Wealthfolio destination drift is zichtbaar wanneer een target
      bereikbaar is.
- [x] Een onbereikbare destination veroorzaakt geen onbegrensde wachttijd en
      wordt expliciet als unavailable/skip gerapporteerd.
- [x] Geen gewone Data health GET wijzigt canonical of destinationdata.
- [x] Destructieve repairacties gebruiken preview, bevestiging, transactie en
      audit logging.
- [x] Bestaande Data health-, control-plane-, connector- en exporter-tests
      blijven groen.
- [x] Nieuwe unit-, PostgreSQL-integratie- en GUI-contracttests dekken alle
      nieuwe checks.
- [x] `ruff`, typecheck, `git diff --check` en de relevante pytest-suites zijn
      groen.

## Verificatiecommando's

Voer minimaal uit:

```bash
uv run pytest -q tests/test_data_health.py tests/test_data_quality.py \
  tests/test_control_plane_api.py tests/test_gui_dashboard.py
uv run pytest -q tests/integration/test_control_plane_phase4_pg.py
uv run pytest -q tests/e2e/test_control_plane_workflow.py
uv run ruff check src tests
uv run pyright src
git diff --check
```

Voeg voor de nieuwe checks aparte gerichte suites toe in plaats van uitsluitend
de volledige regressiesuite te gebruiken.

## Definition of done

De coding agent mag dit verhaal pas op `done` zetten wanneer de checks als
read-only projection betrouwbaar werken, de nieuwe issues in de GUI zichtbaar
en actiegericht zijn, tenant-isolatie en privacytests groen zijn, en minstens
één productieachtige fixture de volledige keten test:

```text
legacy account + actuele account
→ canonical account identity issue
→ holdings/cash/transaction validation
→ Wealthfolio preflight
→ destination parity
→ stabiele issue-ID en veilige herstelactie
```

De productie-dataset mag niet automatisch worden gemerged, verwijderd of
gerebuild als onderdeel van deze implementatie.
