# Cheshire East portal walkthrough

Observed on 15 September 2026.

## Source

- Portal: `https://pa.cheshireeast.gov.uk/planning/index.html?fa=search`
- Shape: custom council register
- Separate route: `?fa=search_map`

The older guessed Idox route is not a source. It returned 404 during the portal census.

## Search

The search form exposed application reference, type, proposal, decision type, applicant and agent names and companies, development code, address components, British National Grid X and Y coordinates, ward, community, valid dates, received dates, proposed committee dates, and decision dates. It stated that appeals are not visible and directed users to the Planning Inspectorate.

A valid-date-from search for 14 September 2026 returned a long result table in the same document. Each visible result exposed reference, application type, location, proposal, optional consultation-close date, and a `View` control. The displayed records included application references such as `26/3180/DSC`, `26/3335/PRIOR-1A`, and `26/3322/NMA`.

The visible `View` control for `26/3322/NMA` accepted focus but did not produce readable detail content during this bounded walkthrough. Detail extraction is therefore a source failure or unresolved client interaction, not an empty record.

## Collection consequences

- Implement the custom form field names and retain every native classification value.
- Run valid, received, and decision windows independently. Proposed committee dates are an additional change signal, not a replacement for application dates.
- Convert supplied British National Grid coordinates to WGS84 while retaining the native X and Y values.
- Keep appeals explicitly unavailable from this register and link to the separate authoritative source when appeal collection is added.
- Treat the same-document result table as a bounded enumeration whose total and pagination rules still need inspection.
- Do not claim application, document, or comment completeness until a detail route succeeds.

## Verification status

`VERIFIED` for the search field inventory and one valid-date result table. `INCONCLUSIVE` for result count, pagination, detail retrieval, documents, comments, and incremental refresh.
