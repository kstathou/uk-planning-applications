# Camden portal walkthrough

Walkthrough date: 15 September 2026.

## Sources

- Current search: `https://accountforms.camden.gov.uk/planning-search/`
- Northgate Planning Explorer: `https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer/`
- Document service: `https://camdocs.camden.gov.uk/CMWebDrawer/PlanRec`

The current search covers applications made since 1 January 2010. It links to the Northgate search for older records. A result redirects to a Northgate detail record. Documents live on a third service.

## Discovery

The current search uses a JSF POST with a search term and `javax.faces.ViewState`. The observed exact-reference search for `2026/2706/L` returned one result. The result included the address, reference, status, decision date, application type, and proposal.

The search supports status-change sorting but does not expose a date range on its first page. Incremental discovery must inspect the search requests and retain Northgate or weekly-list coverage where the current search cannot prove a bounded enumeration.

The Northgate `GeneralSearch.aspx` form separately exposes received, valid, and
decision date ranges plus source-owned status values. The qualification inventory
uses those three date queries followed by `REGISTERED` and `APPEAL LODGED` status
queries when older open cases are requested. Each result page reconciles its
displayed total, ten-row span, numeric Northgate locators, and forward pager.

## Application record

The Northgate detail page identified the record by an internal numeric key. It exposed registered, consultation, committee, decision, appeal, application type, development type, proposal, current status, applicant, agent, ward, British National Grid coordinates, case officer, determination level, land uses, and a map link.

Related pages covered dates, checks, meetings, constraints, documents, and consultees. The document service query used the public reference rather than the Northgate numeric key.

The observed coordinates were easting `530748` and northing `182755`. Normalisation must convert supplied grid coordinates to WGS84 while retaining the native pair.

## Completeness rules

The current search, Northgate detail pages, and document service are separate sources in one authority package. A successful detail page does not prove document completeness. A failure in the document service must retain earlier document metadata and mark the documents section failed.

The current search explicitly limits its coverage to records since 2010. Older records use the legacy search and need a separate covered period.

## Known limits

The exact-reference walkthrough covered one decided record. The later live
qualification proved the first ten rows of a 331-record received-date query and
four matching detail and document-index paths, but Cloudflare repeatedly blocked
the next ordinary Northgate detail navigation. Complete date-window discovery,
older-open enumeration, comments, immediate rerun behavior, and later weekly
cycles therefore remain unqualified.

## Request contract capture

The exact-reference path was rechecked on 16 September 2026. The JSF form
posts `searchForm`, `searchForm:searchTermInput:textField`,
`searchForm:SubmitButton:button`, and `javax.faces.ViewState` as multipart form
data to its session-qualified `index.xhtml` action. The result URL retained the
search term, page, and sort order. Its record link redirected through
`/NECSWS/Redirection/redirect.aspx?linkid=EXDC&PARAM0=681726` to the Northgate
standard-details route.

The Northgate response used a `.dataview` record whose labelled fields included
the reference, address, application and development types, proposal, current
status, published parties, ward, BNG easting and northing, appeal state, case
officer, determination level, and land uses. Dates, checks, meetings,
constraints, consultees, and related documents were separate links keyed by the
same Northgate numeric identifier.

The document service query
`/CMWebDrawer/PlanRec?q=recContainer:"2026/2706/L"` reported 16 records with
created date, title, document type, and an inline source link. The capture read
that index only and did not retrieve any attachment body. Exact-reference
extraction and the observed document index are now verified at request level;
bounded discovery, comments, pagination, and incremental changes remain open.

## Implemented boundary

The authority adapter now resolves an explicit reference through the captured
ordered JSF form fields, retains the Northgate numeric locator, parses the
labelled detail record, and reconciles the CMWebDrawer result count before
retaining document metadata. It also owns a typed five-query Northgate discovery
inventory, validates result counts and pagers, and persists a full-prefix
checkpoint that can be replayed with fresh session tokens. Visible Chrome keeps
the planning and document services in separate pages, allows managed-challenge
scripts, blocks attachment paths and image or media bodies, and emits a specific
error when a challenge does not clear within 60 seconds. Published empty
proposal values and the portal's explicit no-public-documents result remain
distinguishable from parse or retrieval failure. Public comments remain
unavailable.

## Live qualification attempt

The inclusive 18 August to 16 September 2026 bootstrap plus older-open inventory
was attempted and resumed five times on 16 September. The durable checkpoint is
non-terminal at query 0, offset 10: the source reported 331 received-date
results, ten identities were queued, and four applications completed detail and
document-index extraction. The database contains four native, application, and
document versions, no failed current sections, one pending retry, and no
unmapped records.

Across those bounded attempts the transport recorded 12 successful top-level
requests, 528,696 transferred bytes, 257,849 ms of browser time, and zero
attachment-body requests. SQLite integrity is `ok`; all eight compressed
evidence captures passed registration, gzip, and digest checks. The final run
failed with `CamdenChallengeTimeoutError` after a normal visible-Chrome detail
navigation remained on Camden's Cloudflare managed challenge for 60 seconds.
The terminal checkpoint and immediate zero-I/O rerun are not claimed. The two
later weekly cycles due 23 and 30 September remain pending behind the incomplete
bootstrap. The sanitized receipt is
`.audit/camden-live-blocker-2026-09-16.json`; raw HTML and the resumable database
remain outside Git because they contain source records.

Reproduce the boundary with `scripts/qualify_camden.py --confirm-live
--data-dir <empty-directory> --start 2026-08-18 --end 2026-09-16
--include-open`; add `--resume` for every later attempt against that directory.
