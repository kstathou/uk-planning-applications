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

