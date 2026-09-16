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

This walkthrough covered one current tree application and one weekly validated list. It did not yet prove decided-search behavior, older open enumeration, comment pagination, document pagination, or live retry behavior.

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
