# Bestemmingen: optionele consumenten van de persoonlijke datalake

finance-sync is de canonieke, self-hosted persoonlijke datalake. Banken,
brokers en officiële imports worden hier genormaliseerd opgeslagen. Een
bestemming is optioneel: een ontbrekende, gepauzeerde of falende bestemming
blokkeert ingestie, read-API's en backups nooit.

## Eén lokale eigenaar

Een installatie heeft één provisioned eigenaar; self-registration en de
huishoud-/uitnodigingsroutes zijn niet onderdeel van de normale app-surface.
De technische tenantkolom blijft uitsluitend een interne datapartitionering.

## Wizard

Open **Bestemmingen** in het control panel en kies **Bestemming toevoegen**.
De wizard kiest een type, bewaart de self-hosted verbinding, toont een
read-only preview van de gekozen data en activeert de consument. Het testen
van Wealthfolio of Actual Budget doet alleen een authenticatie-/leesprobe; er
worden geen remote accounts, activiteiten, transacties of holdings aangemaakt.
Na activatie opent **Planning** op de bestemmingskaart het gekoppelde schema
in **Sync Runs**. Een handmatige **Sync nu** gebruikt dezelfde opgeslagen
bestemming en verandert het schema niet.

Een bestemming heeft een eigen accountscope. Zonder geselecteerde accounts
gebruikt zij alle actieve rekeningen van de lokale eigenaar; er is geen
extra zichtbaarheidsfilter. Delivery-cursors en accountmaps zijn per
bestemming gescheiden, zodat twee Actual Budget- of Wealthfolio-bestemmingen
onafhankelijk opnieuw kunnen uitvoeren.

Wealthfolio en Actual Budget gebruiken een HTTPS-URL. HTTP is alleen toegestaan
voor `localhost`, `.local` en private IP-adressen, zodat een gewone publieke
verbinding niet per ongeluk onversleuteld wordt geconfigureerd.

## Secrets

Wachtwoorden en tokens staan nooit in de zichtbare bestemmingconfiguratie. Ze
worden met de deployment `MASTER_ENCRYPTION_KEY` versleuteld opgeslagen en zijn
niet opvraagbaar via de API, UI, logs of foutmeldingen. Bij wijzigen laat een
leeg secretveld het bestaande secret ongewijzigd.

## Jupyter

Een Jupyter-bestemming vereist geen Jupyter-server. Bij de eerste activatie
maakt finance-sync één read-only API-key en geeft die precies eenmaal terug,
samen met een starter-notebook. Bewaar de sleutel als
`FINANCE_SYNC_JUPYTER_TOKEN`; finance-sync bewaart de plaintext sleutel niet.
Het notebook gebruikt de normale API met uitsluitend read-permissions en nooit
directe database- of beheercredentials.
De starter verstuurt deze waarde als de `X-API-Key`-header (niet als een JWT
Bearer-token).
Wanneer je in de wizard rekeningen selecteert, geldt die selectie ook voor de
Jupyter API-key; accounts, transacties en holdings buiten die scope zijn via
die key niet leesbaar. Zonder selectie kan de lokale eigenaar bewust alle
eigen actieve rekeningen beschikbaar maken; die allowlist wordt bij activatie,
sleutelrotatie of het opslaan van een gewijzigde selectie opnieuw bepaald.

De starter is opnieuw op te halen via
`GET /api/v1/destinations/{target_id}/jupyter-notebook`. Die download draagt
consumer contract-versie `v1`; alleen de eenmalig getoonde API-key moet lokaal
veilig worden bewaard of via **Sleutel roteren** worden vervangen.

Verwijderen van een Jupyter-bestemming trekt die API-key in. Verwijderen of
pauseren van elke bestemming beïnvloedt de canonieke datalake niet.
