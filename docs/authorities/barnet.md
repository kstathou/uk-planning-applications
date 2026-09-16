# Barnet portal walkthrough

Walkthrough date: 15 September 2026.

## Sources

- Council entry page: `https://www.barnet.gov.uk/planning-and-building-control/planning-applications-and-permissions/view-search-and-comment`
- IDOX Public Access: `https://publicaccess.barnet.gov.uk/online-applications/`

The portal identifies itself as IDOX Public Access. The observed workflow did not require a login.

## Discovery

The weekly-list page starts with a GET request to `search.do?action=weeklyList`. The response supplies session and CSRF state. The form posts to `weeklyListResults.do?action=firstPage` with a ward, a week, and either `DC_Validated` or `DC_Decided`.

The observed week beginning 14 September 2026 returned 37 validated applications. The result page displayed 10 records and linked to four pages. Each record exposed a portal key, reference, proposal, address, status, received date, and validated date.

The portal also has simple, advanced, monthly, current, property, and map searches. The implementation must use overlapping date windows and exhaust every result page.

## Application record

Application `TCP/0564/26` exposed these detail sections:

- Summary.
- Further information.
- Contacts.
- Important dates.
- Comments with a displayed count.
- Constraints.
- Documents with a displayed count.
- Related cases with a displayed count.
- Map.

The summary contained the portal reference, an alternative reference, received and validated dates, address, proposal, status, appeal status, and appeal decision.

Each tab uses `applicationDetails.do` with the portal key and an `activeTab` value. The documents tab links to attachment bodies. Collection must keep those links and metadata without following the attachment requests.

## Completeness rules

The displayed count and the enumerated rows must agree for comments, documents, and related cases. A zero count is empty only after the tab loads successfully. A failed tab remains failed and must not erase an earlier successful version.

The portal depends on a server-side session. Fixture captures must retain the request sequence but remove CSRF values, cookies, names, and free-text public comments.

## Known limits

The implementation now covers validated and decided weekly searches, four
native older-open case states, five native active-appeal states, and an exact
received-date range for the 30-day bootstrap. The first qualification attempt
did not reach every partition because the official source rate-limited detail
collection. Comment and document pagination also remain unproved on a
non-empty live section.

## Request contract capture

The weekly-list request was rechecked on 16 September 2026. The form posts to
`weeklyListResults.do?action=firstPage` and carries the current session cookie,
the hidden `_csrf` value, `searchCriteria.ward`, `week`, `dateType`, and
`searchType`. The observed `dateType` values were `DC_Validated` and
`DC_Decided`.

Result rows use `li.searchresult`. Each summary link carries the portal key in
`applicationDetails.do?keyVal=...&activeTab=summary`; the same row publishes the
human reference, proposal, address, status, received date, and validated date.
Pagination uses `pagedSearchResults.do?action=page&searchCriteria.page=...` and
depends on the same server-side session.

The application summary uses `#simpleDetailsTable` with row labels and values.
The document index uses `table[summary="Documents"]`; attachment URLs are the
view links in its rows. This capture read only the metadata table. It did not
open an attachment, preview, archive, or bulk-download control. Comment counts
and the public and consultee comment routes are separate tabs, so a zero total
must be confirmed from the successfully loaded tab rather than inferred from
the summary page alone.

The older-open path was also checked through `search.do?action=currentList`.
Its form posts the current session fields to
`currentListResults.do?action=firstPage`, but the live response reported `Too
many results found. Please enter some more parameters.` The route is therefore
not a complete older-open enumeration. A live adapter must report that explicit
cap and use bounded advanced-search partitions before Barnet can satisfy the
bootstrap requirement.

The advanced form posts to `advancedSearchResults.do?action=firstPage`. It
supports application status plus received, validated, committee, decision, and
appeal-decision date ranges. Open-state reconciliation will need explicit
partitions for the source's native states, including `Application Received`,
`Valid Application Received`, `Pending Consideration`, and `Pending Decision`,
with each result count and page set exhausted independently.

## Live adapter smoke

An opt-in smoke on 16 September 2026 confirmed several source details that the
fixture alone could not prove. The site accepted an explicit, honest collector
user agent; its weekly form requires `searchType=Application`, and only the
selected `dateType` radio may be posted. Real result rows label the reference
as `Ref. No:` and report pagination as `Showing 1-10 of N`.

After those corrections the adapter traversed real result pages, persisting a
non-secret checkpoint after every page. The source then returned HTTP 429
before discovery and detail collection completed, and a separate HEAD request
also returned 429. No further requests were made during the cooldown. This is
useful contract evidence, but it is not a completed live bootstrap and Barnet
remains discovery-only. The saved state can resume the same bounded week after
the source has recovered.

## Live qualification attempt

The 30-day qualification command started at 09:13 UTC on 16 September 2026
with the exact inclusive range 18 August through 16 September and active
discovery enabled. It persisted the first 10 unique references from the weekly
validated partition, committed 6 applications with 24 retained evidence
captures, and stopped after 26 successful HTTP responses. The failed run
recorded one pending retry and retained a checkpoint for page 2 of the first
validated weekly partition.

A separate read-only visit to that exact official detail URL showed HTTP 429
with the portal's `Too Many Requests` page. The target contains no receipt and
no attachment URL appears in its evidence table. The advanced older-open and
active-appeal partitions were not reached, so this attempt cannot prove a live
bootstrap. The safe resume target is
`.yimby/qualification-barnet-2026-09-16` and must be reused with `--resume`
only after the official portal recovers.

The committed
[`barnet-qualification-blocker-2026-09-16.json`](../evidence/barnet-qualification-blocker-2026-09-16.json)
is a strict, sanitized aggregate derived from that retained target. It records
the safe scope, request and persistence counts, absent receipt, SQLite
integrity, blocker code, and pending later cycles. Checkpoint and evidence-set
hashes bind those claims to the private retained state without publishing an
application identity, session material, or response body. Export is permitted
only for an open-scope incomplete checkpoint with no qualification lineage;
expected validation failures return one fixed sanitized error code.

The qualification transport now enforces a Barnet-specific ten-second minimum
gap and stops on the first 429 instead of retrying. This reduces load while
preserving the exact page, query, retry, and application state needed for a
later resume. It cannot clear the source's server-side cooldown, so a resume is
allowed only after an ordinary official page is healthy again.

The partial state also exposed Barnet's live card layout for comments. Public
cards contain a distinct comment-body element plus separately displayed name
and address fields. The adapter now retains only the comment body. Consultee
cards without a response body are consultation metadata. A zero response badge
is empty, while a positive badge without exposed response text is unavailable.
The three stored pages that revealed this boundary parse offline as complete,
empty, and unavailable. Their already committed failed observations remain
failed until an official-source refresh collects them again.
