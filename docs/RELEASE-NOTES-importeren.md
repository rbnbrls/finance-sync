# Release notes — geïntegreerde importflow

## Dashboard

- Nieuwe imports starten voortaan op één pagina: **Importeren**.
- De gebruiker kiest eerst de tegenpartij en daarna de beschikbare methode:
  API-koppeling of bestandsimport.
- DEGIRO Pensioen, SaxoInvestor, CSV import en Handmatige uitgaven tonen hun
  eigen bestandsinstructies binnen dezelfde wizard-shell.
- **Bestaande koppelingen beheren** blijft beschikbaar als secundaire actie.
  De oude Importers-sectie is niet langer een primaire gebruikersingang.

## API en historie

- File-imports gebruiken het gemeenschappelijke dispatchcontract.
- Uploadhistorie toont de werkelijke provider per tenant-scoped import-run.
- Preview-, confirm-, idempotentie- en backwards-compatible provider-endpoints
  blijven beschikbaar.
