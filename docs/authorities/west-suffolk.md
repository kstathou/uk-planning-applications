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

The original walkthrough covered one weekly validated list, one current record,
and its document index. A follow-up live adapter run on 16 September 2026 also
exhausted the two-page decided list and fetched one summary plus its safe child
sections. It did not prove older open enumeration, related-case detail, retries,
incremental updates, or a complete persisted bootstrap of every discovered
record.

## Request contract capture

The weekly-list request was rechecked on 16 September 2026. It posts to
`weeklyListResults.do?action=firstPage` with the current session, `_csrf`,
`searchCriteria.parish`, `searchCriteria.ward`, `week`, `dateType`, and
`searchType`. The hidden form value was `searchType=Application`; the adapter
preserves that portal-owned value. The date-type values were `DC_Validated` and
`DC_Decided`.

The live adapter run found three validated applications and fourteen decided
applications for the week beginning 14 September 2026. The validated response
was an under-capacity first page without a displayed total. Its selected page
capacity and absence of pagination established terminality. The decided
response reported `Showing 1-10 of 14` and was exhausted across two pages.
Summary links carried the portal key in
`applicationDetails.do?keyVal=...&activeTab=summary`; each row also published
the human reference, proposal, address, received date, validated date, status,
and comment-open marker. The smoke fetched `DC/26/1388/TCA`, completed without a
detail error, transferred no attachment body, and retained a resumable
checkpoint. This confirms weekly discovery, pagination, and one bounded detail
path. It does not establish a complete persisted bootstrap, older-open coverage,
or incremental operation.
