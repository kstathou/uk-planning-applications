# Cornwall portal walkthrough

Observed on 15 September 2026.

## Source

- Portal: `https://planning.cornwall.gov.uk/online-applications/`
- Shape: Idox Public Access
- Recorded application: `PA26/06144`, portal key `TKSDKZFGKW900`

## Weekly discovery

The weekly-list form exposed parish, ward, week, and validated-or-decided controls. Its week selector retained more than a year of published weeks. The validated list for the week beginning 14 September 2026 reported 34 records across four pages at the default 10 records per page. The page allowed page sizes of 5, 10, 20, 50, or 100.

Each visible result exposed the portal key, reference, proposal, address, validation date, status, and sometimes an open-for-comment marker. The first page included current, decided, awaiting-decision, and unknown statuses.

## Application detail

The summary for `PA26/06144` exposed the reference, alternative reference `PP-15201309`, validation date, address, proposal, status, and empty appeal fields. Its visible sections were:

- details with summary, further information, contacts, and important dates;
- comments with an explicit count of zero;
- 21 constraints;
- 16 documents;
- map;
- one related case.

The document index exposed all 16 rows on one page. Each row included publication date, type, optional drawing number, description, a metadata-only viewer or measurement link, and a direct PDF link. A bulk-download control was also present. No PDF, viewer body, archive, or preview was opened.

## Collection consequences

- Reconcile the weekly list with advanced received, validated, decision, and change-date searches.
- Exhaust result pagination and compare queued references with the reported count.
- Retain the portal key as source-local routing data and the alternative reference as an alias.
- Record the comment section as empty only when the zero count and successfully loaded section agree.
- Retain document dates, types, drawing numbers, descriptions, and source links as metadata. Never select bulk download or open document bodies.
- Keep appeal and related-case sections distinct even when their current values are empty.

## Verification status

`VERIFIED` for one weekly discovery page, one application summary, the visible section counts, and the complete 16-row document index. Further-detail, contact, date, constraint, map, related-case, decided-list, and incremental-refresh paths remain open.

## Request contract capture

The weekly-list request was rechecked on 16 September 2026. It posts to
`weeklyListResults.do?action=firstPage` with the current session, `_csrf`,
`searchCriteria.parish`, `searchCriteria.ward`, `week`, `dateType`, and
`searchType`. The observed date-type values were `DC_Validated` and
`DC_Decided`.

The live result used `li.searchresult` rows and published 34 records across
four pages. Summary links carried the portal key in
`applicationDetails.do?keyVal=...&activeTab=summary`; later pages used
`pagedSearchResults.do?action=page&searchCriteria.page=...`. The result rows
also exposed the human reference, proposal, address, validated date, and
status. This confirms the request and pagination contract only; it does not
close the open incremental and child-section checks above.
