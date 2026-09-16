# Durham County Council portal walkthrough

Observed on 15 September 2026.

## Source

- Portal: `https://publicaccess.durham.gov.uk/online-applications/`
- Shape: Idox Public Access
- Recorded application: `DM/26/02422/LB`, portal key `TL7J9VGDLR600`

## Weekly discovery

The weekly-list form exposed parish, ward, week, and validated-or-decided controls. It also linked to a separate current-applications list. The validated list for the week beginning 14 September 2026 displayed nine records on one page.

Each visible result exposed status, proposal, address, reference, received date, validated date, and sometimes an open-for-comment marker. Several records were received before the selected week, which confirms that received date and validation date cannot be treated as equivalent discovery windows.

## Application detail

The summary for `DM/26/02422/LB` exposed the reference, alternative reference `PP-15205843`, received and validated dates, address, proposal, status, and explicit unavailable appeal values. Its visible sections were details, comments, six documents, one related case, and map.

The comments area contained a make-comment page and a `Consultee Comments (0)` subtab. It stated that submitted comments are published with name and address and required login for submission. The walkthrough did not log in or submit anything. The page did not expose a public-representation list for this record, so public comment text remains unavailable rather than empty.

The document index exposed all six rows. Metadata included publication date, document type, description, optional measurement link, and a direct PDF or image link. One row was a neighbour-notification list. No document, image, viewer, archive, or preview body was opened.

## Collection consequences

- Search both validated and decided weeks, and use received-date and current-case searches for reconciliation.
- Keep received and validated dates as separate native and normalised fields.
- Preserve `Not Available` as a native appeal value and map it to an explicit unavailable state when appropriate.
- Treat public comment text as unavailable for this record. A zero consultee count does not prove that every comment category is empty.
- Retain all document metadata, including image links, without retrieving bodies or using the bulk-download control.
- Treat neighbour-notification documents as document metadata, not as published party records.

## Verification status

`VERIFIED` for the recorded weekly list, one application summary, comment-section shape, and complete six-row document index. Further-detail, contact, date, related-case, map, decided-list, pagination, and incremental-refresh paths remain open.

## Request contract capture

The weekly-list request was rechecked on 16 September 2026. It posts to
`weeklyListResults.do?action=firstPage` with the current session, `_csrf`,
`searchCriteria.parish`, `searchCriteria.ward`, `week`, `dateType`, and
`searchType`. The date-type values were `DC_Validated` and `DC_Decided`.

The live validated response exposed nine `li.searchresult` rows without a
second result page. Each summary link carried the portal key in
`applicationDetails.do?keyVal=...&activeTab=summary`, while the row published
the human reference, proposal, address, received date, validated date, status,
and comment-open marker. This confirms the current weekly request and row
contract, not the open decided, pagination, and incremental checks above.
