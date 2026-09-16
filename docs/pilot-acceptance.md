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
| Haringey | Verified | Dynamic seven-day pagination, detail, explicit empty comments, and six file rows recorded | Authority-owned browser adapter for the rolling seven-day path; arbitrary windows and older-open unresolved | Open | Open |
| Devon County Council | Verified | Discovery and detail routes recorded; child sections partial | Rolling 90-day received adapter with disclaimer handling and hidden document metadata | Open | Open |
| Peak District National Park Authority | Verified | AssureLive form, five-query discovery, detail, and windowed search and document pagination recorded | Exact 30-day Received, Validated, and Decided queries plus any-time REGISTERED and APPEAL LODGED; complete detail and document metadata; public comments unavailable | Verified on 16 September 2026 with 377 references and applications | Pending for 23 and 30 September 2026 |
| Arun | Verified | Bounded received-date route, result cap, detail, and document index recorded | Received-date and Show All adapter; document action and older-open unresolved | Open | Open |
| Old Oak and Park Royal Development Corporation | Verified | Official Registered/Determined searches, client-side pagination, detail, 643-row sampled document index, and public responses recorded | Exact three-query HTTP adapter with complete detail, document-metadata, and response collection | Verified on 16 September 2026: 55 applications, zero failed sections/retries/attachment bodies, and zero-network rerun | Open; approximately 23 and 30 September 2026 |
| Dorset | Verified | Partial map-client boundary recorded | Client boundary unresolved | Open | Open |
| Cheshire East | Verified | Form, result table, and numeric View locators recorded; same-day fidelity contradicted | Valid-date-from discovery adapter stops explicitly before unverified total, pagination, window fidelity, or detail | Open | Open |
| Blackburn with Darwen | Verified | Source blocked | Blocked | Open | Open |
| Birmingham | Verified | Source blocked | Blocked | Open | Open |
| Leeds | Verified | Discovery route recorded; detail blocked | Live weekly discovery completed with 293 unique references across both date types; detail remains unverified | Open | Open |
| Cornwall | Verified | Discovery, detail, document index, and empty comment count recorded | Real HTTP adapter implemented; live run pending | Open | Open |
| Durham County Council | Verified | Discovery, detail, document index, and unavailable public comment text recorded; portal blocked on 16 September 2026 | Discovery-only adapter; exact received-date and older-open routes unproved during outage | Open | Open |
| West Suffolk | Verified | Discovery, detail, document index, and representation metadata recorded | Ten weekly and eight active-state partitions completed with strict pagination, exact form values, persisted details, and no attachment bodies | Verified, 730 applications, receipt dated 16 September 2026 | Open |

The deterministic column proves package ownership, typed native payloads, parsing, normalisation, source evidence retention, semantic idempotence, explicit section completeness, and attachment-body blocking through sanitised fixtures. It does not substitute for browser-to-scraper agreement.

As of 16 September 2026, West Suffolk, OPDC, and Peak District have completed
verified live bootstraps. West Suffolk's versioned receipt proves 730
discovered references, 730 persisted applications, terminal checkpoint
coherence, no pending retries,
no failed current sections, database integrity, retained evidence-path
presence, no unmapped records, no attachment-body requests, and an immediate
zero-request rerun. OPDC's sanitized
[committed receipt](evidence/opdc-qualification-2026-09-16.json) proves 55
persisted applications, exact terminal discovery, complete implemented
sections, local database and evidence integrity, 103 distinct retained content
digests, 165 ordered application-to-capture associations, and an immediate
zero-network rerun. Its SHA-256 commitment covers each application's source
identity and ordered capture URL, media type, and content digest without
publishing the identity inventory. Peak District's privacy-safe
[committed receipt](evidence/peak-district-qualification-2026-09-16.json)
records 377 applications, 95 decision dates, zero retry entries, 1,696
cumulative durable acquisition requests, 1,299 ordered current capture
associations, integrity commitments for all 1,560 retained evidence rows, a
persisted live-ready HTTP manifest, and an immediate zero-network rerun. The
other twelve authorities still require verified live bootstraps. All fifteen
still require two successful later weekly cycles, so operational qualification
remains zero of fifteen.

The Barnet smoke is incomplete because the source returned HTTP 429 during
pagination; its saved checkpoint permits a later bounded resume without
repeating completed pages. Blocked and partial authorities remain in coverage
denominators and failure reporting until their checks succeed.

Current source-health checks on 16 September 2026 returned an empty reply from
Cornwall and timed out at Durham after bounded retries. West Suffolk and Leeds
were healthy for completed weekly discovery smokes. The earlier smoke results
did not move the live-bootstrap cells because they checkpointed discovery
without persisting every discovered application through the operational
collection store. West Suffolk has since passed that boundary through the dated
qualification receipt. Leeds still stops explicitly before its unverified
detail surface.

The later Durham qualification attempt resolved the Public Access host to
`217.23.233.121`, but bounded probes on ports 80 and 443 timed out before an
HTTP response. The council's main planning page remained healthy. The current
advanced form and current-case list could not be captured, so the exact 30-day
received-date route and complete older-open query inventory remain unproved.
No qualification receipt was emitted. Durham remains discovery-only.
