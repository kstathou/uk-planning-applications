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
| Peak District National Park Authority | Verified | Migrated discovery and detail routes recorded; child sections partial | Rolling-week legacy adapter; client-loaded child sections explicitly failed | Open | Open |
| Arun | Verified | Bounded received-date route, result cap, detail, and document index recorded | Received-date and Show All adapter; document action and older-open unresolved | Open | Open |
| Old Oak and Park Royal Development Corporation | Verified | Source blocked | Blocked | Open | Open |
| Dorset | Verified | Partial map-client boundary recorded | Client boundary unresolved | Open | Open |
| Cheshire East | Verified | Form, result table, and numeric View locators recorded; same-day fidelity contradicted | Valid-date-from discovery adapter stops explicitly before unverified total, pagination, window fidelity, or detail | Open | Open |
| Blackburn with Darwen | Verified | Source blocked | Blocked | Open | Open |
| Birmingham | Verified | Source blocked | Blocked | Open | Open |
| Leeds | Verified | Discovery route recorded; detail blocked | Live weekly discovery completed with 293 unique references across both date types; detail remains unverified | Open | Open |
| Cornwall | Verified | Discovery, detail, document index, and empty comment count recorded | Real HTTP adapter implemented; live run pending | Open | Open |
| Durham County Council | Verified | Discovery, detail, document index, and unavailable public comment text recorded | Real HTTP adapter implemented; live run pending | Open | Open |
| West Suffolk | Verified | Discovery, detail, document index, and representation metadata recorded | Live weekly discovery completed for 3 validated and 14 decided applications; one bounded detail fetched with no attachment body | Open | Open |

The deterministic column proves package ownership, typed native payloads, parsing, normalisation, source evidence retention, semantic idempotence, explicit section completeness, and attachment-body blocking through sanitised fixtures. It does not substitute for browser-to-scraper agreement.

As of 16 September 2026, no authority has completed a verified live bootstrap.
The Barnet smoke is incomplete because the source returned HTTP 429 during
pagination; its saved checkpoint permits a later bounded resume without
repeating completed pages. All fifteen authorities still require a live
bootstrap and two successful weekly cycles. Blocked and partial authorities
remain in coverage denominators and failure reporting until those checks
succeed.

Current source-health checks on 16 September 2026 returned an empty reply from
Cornwall and timed out at Durham after bounded retries. West Suffolk and Leeds
were healthy for completed weekly discovery smokes. These results do not move
their live-bootstrap cells because the smokes checkpoint discovery but do not
persist every discovered application through the operational collection store.
West Suffolk fetched one bounded detail; Leeds stopped explicitly before its
unverified detail surface.

The Haringey source-health check at 09:42 Europe/London on 16 September 2026
could not load the register list. At 09:47, both the register index and the
previously recorded detail route reported that the site was temporarily
unavailable. This blocks a current 30-day and older-open completeness
walkthrough. Haringey remains `BROWSER_ONLY`; its live-bootstrap and both later
weekly-cycle cells remain open.

The portal recovered later that morning. The implemented seven-day smoke then
reported 61 records across seven pages with zero attachment-body requests, and
the official user-defined weekly list proved an inclusive selected-date through
selected-date-plus-seven-days interval. The recovered UI still provides no
all-open route or application-status search field, while a broad advanced
search truncates at 250 visible results. Because older-open completeness cannot
be established, the recovery does not change Haringey's `BROWSER_ONLY` status,
does not satisfy live bootstrap, and does not start either later weekly cycle.

A later official-map investigation reconciled the
`planning_current_apps` WFS hit count to 1,445 whole-borough map features and
1,445 unique application links. That proves complete enumeration of the named
map layer, not older-open completeness: its first feature, `HGY/2023/2916`, is
reported by the council register as `Decision Made` on 28 November 2023. With
no published inclusion rule proving that all open cases are present despite
decided and stale records, Haringey remains fail-closed and no qualification
receipt exists.

The official legacy lite-map configuration subsequently exposed a separate
`curr_planning_apps_solo` layer alongside its decided layer. Its whole-borough
WFS and shape counts reconciled at 826 unique PKIDs, while the newer current
layer remained independently countable. This makes the union of the legacy and
Arcus current layers a credible cross-era discovery boundary. It does not make
the bootstrap collectable: the 826 legacy links target a council host that no
longer resolves, the layer publishes no HGY reference, the current register
does not search by PKID, and the public Arcus detail payload exposes no legacy
identifier. Exact address/proposal samples resolve to decided Arcus records,
confirming that the legacy layer is a stale cutover snapshot rather than an
active-status set, but content matching is not an identity crosswalk. Until all
legacy PKIDs have an official zero-ambiguity mapping, live bootstrap and the two
later weekly cycles remain open.

The legacy decided layer was then reconciled independently in 16 throttled map
tiles: its advertised 13,974 features became 13,974 unique, unambiguous
PKID-to-HGY pairs. None of those PKIDs occurs among the 826 legacy-current
PKIDs. The council's decided layer is therefore internally complete but cannot
provide the missing cutover crosswalk. A Salesforce `FULL` guest layout also
exposed only system fields, and the public object-info endpoint rejected guest
access, so no hidden migrated identifier is available through that route.

The authority-specific Haringey qualifier was then run against the requested
30-day-plus-older-open scope. It persisted a failed collection run and returned
the structured failed checks `bounded-30-day-discovery` and
`complete-older-open-inventory` with `HaringeyWindowUnavailableError`. It
created neither a final nor temporary receipt. This gives the blocker a
repeatable operational test without converting partial discovery evidence into
bootstrap or weekly-cycle credit.
