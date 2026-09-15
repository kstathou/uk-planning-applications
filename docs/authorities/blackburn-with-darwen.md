# Blackburn with Darwen portal walkthrough

Observed on 15 September 2026.

## Source

- Portal: `https://planning.blackburn.gov.uk/Northgate/PlanningExplorer/`
- Expected shape: Northgate Planning Explorer

## Observed response

The portal returned a site-maintenance page headed `We'll be back soon!`. The page stated that the service was undergoing maintenance and exposed no search, application, document, or comment controls.

This is a blocked source, not an empty register. No reference, date window, pagination rule, detail route, or child section could be verified.

## Collection consequences

- Report the authority as blocked with a source-maintenance diagnostic.
- Do not advance a discovery checkpoint or infer zero applications.
- Keep retry scheduling bounded and preserve the previous successful observation when the source later becomes available.
- The fixture package may encode the expected authority boundary, but it cannot be described as browser-to-scraper agreement until a live route succeeds.

## Verification status

`VERIFIED` only for the maintenance response. Discovery, extraction, completeness, and incremental refresh remain `INCONCLUSIVE`.
