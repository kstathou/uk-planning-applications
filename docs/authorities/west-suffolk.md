# West Suffolk portal walkthrough

Walkthrough date: 15 September 2026.

## Source

- IDOX Public Access: `https://planning.westsuffolk.gov.uk/online-applications/`

The portal also exposes building-control searches. Those records are outside this pilot.

## Discovery

The weekly-list form uses the standard IDOX session flow. The observed week beginning 14 September 2026 returned three validated applications. The result page exposed each proposal, address, reference, received date, validated date, and comment status.

The first result was `DC/26/1388/TCA`. Its summary exposed an alternative reference, the received and validated dates, address, proposal, status, appeal status, and appeal decision.

## Sections

The record exposed summary, further information, contacts, important dates, comments, constraints, documents, related cases, and map tabs. The document index reported six records and displayed six rows.

Document rows contained a published date, document type, drawing number when present, description, media URL, and measurement URL when supported. The walkthrough read only the index. It did not open any attachment body.

The portal says that representations may take two working days to appear. It publishes representations as documents. The scraper must report comment text as unavailable unless the record exposes it outside the attachment body.

## Completeness rules

The displayed document count and the enumerated rows must agree. A document can be a PDF or an image, so the attachment block cannot depend on a `.pdf` suffix alone.

An empty comments tab does not imply that no representations exist. The document index may contain representation files that policy excludes from body retrieval.

## Known limits

This walkthrough covered one weekly validated list, one current record, and its document index. It did not prove decided searches, older open enumeration, pagination beyond one result page, related-case detail, retries, or incremental updates.

## Request contract capture

The weekly-list request was rechecked on 16 September 2026. It posts to
`weeklyListResults.do?action=firstPage` with the current session, `_csrf`,
`searchCriteria.parish`, `searchCriteria.ward`, `week`, `dateType`, and
`searchType`. The date-type values were `DC_Validated` and `DC_Decided`.

The current validated response exposed three `li.searchresult` rows. Summary
links carried the portal key in
`applicationDetails.do?keyVal=...&activeTab=summary`; each row also published
the human reference, proposal, address, received date, validated date, status,
and comment-open marker. This confirms the current weekly request and row
contract, not the open decided, older-open, retry, or incremental checks above.
