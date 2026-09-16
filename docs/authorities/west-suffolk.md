# West Suffolk portal walkthrough

Walkthrough date: 15 September 2026.

## Source

- IDOX Public Access: `https://planning.westsuffolk.gov.uk/online-applications/`

The portal also exposes building-control searches. Those records are outside this pilot.

## Discovery

The weekly-list form uses the standard IDOX session flow. The observed week beginning 14 September 2026 returned three validated applications. The result page exposed each proposal, address, reference, received date, validated date, and comment status.

The first result was `DC/26/1388/TCA`. Its summary exposed an alternative reference, the received and validated dates, address, proposal, status, appeal status, and appeal decision.

A persisted bootstrap completed on 16 September 2026 for the inclusive window
18 August to 16 September 2026. It exhausted ten weekly queries, covering five
intersecting weeks for both validated and decided dates. It then exhausted four
application-status partitions and four appeal-status partitions for older open
applications. The application statuses were Pending Consideration, Pending
Decision, Received Awaiting Registration, and Pending Appeal Decision. The
appeal statuses were Appeal lodged, Appeal Remitted to Secretary of State, High
Court Appeal Lodged, and Pending Appeal Decision.

The portal-owned value for Appeal Remitted to Secretary of State includes a
trailing space. The scraper preserves that value exactly. Advanced first pages
can expose either an empty hidden page marker or `1`. Both forms are accepted
only on page one. Counted later pages must reconcile their visible page, range,
and capacity evidence. A stale hidden page marker cannot override coherent
visible evidence.

## Sections

The record exposed summary, further information, contacts, important dates, comments, constraints, documents, related cases, and map tabs. The document index reported six records and displayed six rows.

Document rows contained a published date, document type, drawing number when present, description, media URL, and measurement URL when supported. The walkthrough read only the index. It did not open any attachment body.

The portal says that representations may take two working days to appear. It publishes representations as documents. The scraper must report comment text as unavailable unless the record exposes it outside the attachment body.

## Completeness rules

The displayed document count and the enumerated rows must agree. A document can be a PDF or an image, so the attachment block cannot depend on a `.pdf` suffix alone.

An empty comments tab does not imply that no representations exist. The document index may contain representation files that policy excludes from body retrieval.

## Known limits

The original walkthrough covered one weekly validated list, one current record,
and its document index. The persisted bootstrap now proves bounded recent and
older-open discovery, detail persistence, retry state, source evidence, and an
immediate idempotent rerun. It does not prove the two later weekly refreshes,
related-case expansion beyond the recorded fields, or public comment text that
the portal exposes only inside attachment bodies.

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
path.

## Live bootstrap qualification

The committed
[qualification receipt](../evidence/west-suffolk-qualification-2026-09-16.json)
records 730 discovered references and 730 persisted applications. It also
records 730 native versions, 730 application versions, 730 document versions,
no pending retries, no failed current sections, no unmapped records, and no
attachment-body requests. SQLite integrity and retained evidence paths passed.

The successful terminal resume made 76 requests and transferred 3,019,581
bytes. Those figures describe that resume, not the earlier interrupted attempts.
The immediate rerun made zero requests, transferred zero bytes, and left the
qualification counts and version state unchanged. Both recorded runs succeeded.
This satisfies live bootstrap acceptance. The two genuinely later weekly
refreshes remain open.
