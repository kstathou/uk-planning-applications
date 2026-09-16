# Birmingham portal walkthrough

Observed on 15 September 2026.

## Source

- Portal: `https://eplanning.birmingham.gov.uk/Northgate/PlanningExplorer/`
- Expected shape: Northgate Planning Explorer

## Observed response

The portal returned `HTTP Error 503. The service is unavailable.` It exposed no search, application, document, or comment controls.

This is a source outage, not an empty register. No reference, date window, pagination rule, detail route, or child section could be verified.

## Collection consequences

- Report the authority as blocked with an HTTP 503 diagnostic.
- Honour any future `Retry-After` response and use bounded retries.
- Do not advance a discovery checkpoint or replace existing sections with empty values.
- The fixture package may encode the expected authority boundary, but it cannot be described as browser-to-scraper agreement until a live route succeeds.

## Verification status

`VERIFIED` only for the 503 response. Discovery, extraction, completeness, and incremental refresh remain `INCONCLUSIVE`.
