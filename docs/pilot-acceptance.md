# Pilot acceptance ledger

This ledger separates deterministic fixture proof from live portal acceptance. `Verified` means the dated walkthrough established the stated path. `Partial` means the path exists but one or more required branches remain unresolved. `Blocked` means the source returned an outage, maintenance page, or unusable client document. `Open` means the check has not yet been completed.

## Evidence levels

These levels are cumulative and separate implementation evidence from current
source health.

| Level | Required evidence |
|---|---|
| Package present | Authority-specific code and a versioned native schema exist. |
| Deterministically verified | Meaningful fixtures prove extraction, normalisation, incremental behavior, and failure handling. |
| Browser verified | A dated walkthrough verifies only its documented routes and sections. |
| Live collection verified | The real adapter completes bootstrap, persists results, exhausts pagination, and agrees with the browser evidence. |
| Operationally qualified | Live bootstrap is followed by successful refreshes approximately 7 and 14 days later. |

Current source health is recorded independently as unverified, healthy,
partial, or blocked. A previously verified adapter can later be blocked by an
outage. Same-day reruns and simulated dates do not satisfy the two weekly-cycle
requirement.

| Authority | Fixture package | Recorded source evidence | Real adapter boundary | Live bootstrap | Two weekly cycles |
|---|---|---|---|---|---|
| Barnet | Verified | Weekly, detail, and child routes recorded; older-open result cap recorded | Bounded weekly adapter; smoke reached paged discovery before HTTP 429 | Open | Open |
| Camden | Verified | Detail and 16-row document index recorded; comments open | Exact-reference JSF, Northgate detail, and document-index adapter; bounded discovery explicitly unavailable | Open | Open |
| Haringey | Verified | JavaScript detail, comment, and file tabs recorded | Browser-only | Open | Open |
| Devon County Council | Verified | Discovery and detail routes recorded; child sections partial | Rolling 90-day received adapter with disclaimer handling and hidden document metadata | Open | Open |
| Peak District National Park Authority | Verified | Migrated discovery and detail routes recorded; child sections partial | Rolling-week legacy adapter; client-loaded child sections explicitly failed | Open | Open |
| Arun | Verified | Bounded received-date route, result cap, detail, and document index recorded | Received-date and Show All adapter; document action and older-open unresolved | Open | Open |
| Old Oak and Park Royal Development Corporation | Verified | Source blocked | Blocked | Open | Open |
| Dorset | Verified | Partial map-client boundary recorded | Client boundary unresolved | Open | Open |
| Cheshire East | Verified | One valid-date query recorded; detail blocked | Detail boundary unresolved | Open | Open |
| Blackburn with Darwen | Verified | Source blocked | Blocked | Open | Open |
| Birmingham | Verified | Source blocked | Blocked | Open | Open |
| Leeds | Verified | Discovery route recorded; detail blocked | Weekly discovery adapter; detail explicitly unavailable | Open | Open |
| Cornwall | Verified | Discovery, detail, document index, and empty comment count recorded | Real HTTP adapter implemented; live run pending | Open | Open |
| Durham County Council | Verified | Discovery, detail, document index, and unavailable public comment text recorded | Real HTTP adapter implemented; live run pending | Open | Open |
| West Suffolk | Verified | Discovery, detail, document index, and representation metadata recorded | Real HTTP adapter implemented; live run pending | Open | Open |

The deterministic column proves package ownership, typed native payloads, parsing, normalisation, source evidence retention, semantic idempotence, explicit section completeness, and attachment-body blocking through sanitised fixtures. It does not substitute for browser-to-scraper agreement.

As of 16 September 2026, no authority has completed a verified live bootstrap.
The Barnet smoke is incomplete because the source returned HTTP 429 during
pagination; its saved checkpoint permits a later bounded resume without
repeating completed pages. All fifteen authorities still require a live
bootstrap and two successful weekly cycles. Blocked and partial authorities
remain in coverage denominators and failure reporting until those checks
succeed.
