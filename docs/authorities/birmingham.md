# Birmingham source walkthrough

Observed on 15 and 16 September 2026.

## Sources

- Legacy portal: `https://eplanning.birmingham.gov.uk/Northgate/PlanningExplorer/`
- Official ArcGIS layer: `https://maps.birmingham.gov.uk/server/rest/services/mybrummap/mybrummap_Planning_OGCServices/MapServer/12`
- ArcGIS layer name: `Post 1990 Planning Application`

The legacy Northgate portal returned `HTTP Error 503. The service is
unavailable.` The official Birmingham City Council ArcGIS layer responded and
supports JSON, pagination, ordering, distinct values, and statistics.

## Current-window evidence

The ArcGIS layer reported 254,064 records. Its newest received application was
`2026/05750/PA`, received on 15 September 2026. An exact inclusive 30-day
window from 18 August through 16 September contained 70 records. Ordered
pagination returned 25, 25, and 20 records. The 70 unique references and 70
unique object identifiers agreed with the count endpoint, and the third page
was terminal.

The current latest record proves recency, but it does not prove population
continuity. The layer contains 5,802 records received in calendar 2025, 453 in
July 2026, 121 in August 2026, and 70 in the rolling 30-day window. The source
does not explain the sharp recent decline. Qualification therefore records
freshness as not proven rather than treating a current maximum date as proof
of complete current coverage.

The layer also reported 27 October 2026 as its maximum accepted date. That is
future-dated relative to the 16 September qualification and is recorded as a
source-data anomaly.

## Older-open and appeal boundary

The layer has no explicit application status or current appeal status field.
There were 3,276 records with a null `APPLICATION_DECISION`. Null application
decision and null decision date agreed, but those records do not represent a
safe open-case set. Historical rows include issued post-decision amendments.

Even the narrower condition with null application decision, decision date,
and issued date returned 1,419 records from 2009 onward. Its oldest records
included enforcement and master-plan entries with dismissed or withdrawn
appeals. A null decision predicate would therefore mix open applications with
closed appeals, post-decision work, and records outside the initial planning
register scope.

The layer exposes appeal decision and appeal decision date, but it does not
expose lodged or current appeal status. It cannot prove the active-appeal
refresh set.

## Child-section boundary

The metadata reports no attachments and no relationships. The field inventory
does not expose document metadata, public comments, conditions,
consultations, or related cases. These sections are not exposed, rather than
verified empty. No attachment body was requested.

## Collection consequences

- Keep Birmingham at `blocked` live readiness.
- Preserve the ArcGIS responses and query inventory as qualification evidence.
- Use the ArcGIS layer only to prove its bounded current-window subset.
- Reject the synthetic Northgate fixture routes before any non-fixture request.
- Do not infer older-open applications, active appeals, or empty child sections.
- Do not advance a collection checkpoint or schedule weekly collection.

## Verification status

`VERIFIED` for the ArcGIS schema, dated freshness observations, exact 30-day
pagination, and the recorded semantic counterexamples. Complete source
freshness, older-open enumeration, active appeals, detail extraction, and
required child sections remain `INCONCLUSIVE`. Live bootstrap and both later
weekly cycles remain open.
