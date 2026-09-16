# Leeds portal walkthrough

Observed on 15 September 2026.

## Source

- Portal: `https://publicaccess.leeds.gov.uk/online-applications/`
- Shape: Idox Public Access
- Register boundary: planning applications only. Building control and licensing are separate services.

## Weekly discovery

The weekly-list form exposed parish, ward, week, and date-type controls. The date type distinguishes applications validated in the selected week from applications decided in it. The page warned that the published weekly list does not necessarily contain every record with a matching validation or decision date, so advanced date searches are also required for reconciliation.

The current week beginning 14 September 2026 returned an explicit `No results found` response. The preceding week beginning 7 September 2026 returned 156 records. Results were paginated at 10 per page, with 16 pages implied by the reported count. The page allowed 5, 10, 20, 50, or 100 results per page.

Each result exposed a portal key, reference, proposal, address, validation date, status, and sometimes an open-for-comment marker. One visible record was reference `26/05013/FU`, keyed by `TKZTIUJBL8700`.

## Detail path

Opening the visible `26/05013/FU` detail link returned the portal's own error page with `Unable to perform this task. A remote exception occurred.` No retry or alternate detail was used during this bounded walkthrough.

This is a failed detail request, not an empty application. Discovery is verified
for the recorded weekly-list path. Extraction, documents, and comments remain
inconclusive until the detail service succeeds. Complete weekly pagination is
verified by the live adapter run below.

A live adapter smoke on 16 September 2026 exhausted both validated and decided
weekly lists for the week beginning 7 September. It retained 293 unique
references after deduplicating overlap between the date types. The adapter then
stopped with `LeedsDetailUnverifiedError` before treating the unverified detail
surface as extracted data.

## Collection consequences

- Use the weekly list for enumeration, but reconcile with advanced received, validated, and decision searches.
- Exhaust all result pages and check the reported count against queued references.
- Treat the portal key as source-local routing data and the planning reference as an alias, not as a cross-authority identity key.
- Preserve an explicit source failure when the detail endpoint reports a remote exception.
- Leeds states elsewhere in the public service that comment text is not published. Model that as unavailable when confirmed for a successfully loaded record, not as an empty comment set.

## Verification status

`VERIFIED` for the two weekly-list outcomes and the 156-record pagination contract. `INCONCLUSIVE` for the application detail and every child section because the selected record returned a remote exception.

## Request contract capture

The weekly-list request was rechecked on 16 September 2026. It posts to
`weeklyListResults.do?action=firstPage` with the current session, `_csrf`,
`searchCriteria.parish`, `searchCriteria.ward`, `week`, `dateType`, and
`searchType`. The date-type values were `DC_Validated` and `DC_Decided`. The
adapter preserves the form-supplied `searchType` value as portal-owned state
rather than replacing it with an inferred weekly-list value.

Selecting the week beginning 7 September 2026 again produced 156 records. The
response used `li.searchresult` rows and
`pagedSearchResults.do?action=page&searchCriteria.page=...`; the first ten page
links and a next-page link were visible, so enumeration must continue until the
reported 156 rows are queued. Each summary link carried the portal key in
`applicationDetails.do?keyVal=...&activeTab=summary`, while the result row
published the human reference, proposal, address, validated date, and status.
The live smoke subsequently exhausted both date-type queries, retained 293
unique references, and completed with zero attachment-body requests. It made no
claim about detail or child-section completeness, and it did not persist a full
application bootstrap.
