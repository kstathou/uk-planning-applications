# Devon County Council portal walkthrough

Walkthrough and qualification date: 16 September 2026.

## Source and scope

- Planning register: `https://planning.devon.gov.uk/`
- Covered records: minerals, waste, and county council development.
- Qualification window: 18 August through 16 September 2026, inclusive.
- Older-open policy: planning applications with `Outstanding=true`.

The official register presents a copyright and data-use disclaimer before a
protected route when the session has not accepted it. The acceptance form posts
to `/Disclaimer/Accept`; a disclaimer is never interpreted as an empty search or
an application record.

## Exact discovery contract

The adapter fetches `/Search/Advanced`, requires one POST form with action
`/Search/Results`, and preserves the form's successful controls in DOM order.
The captured controls include the verification token, `AdvancedSearch`, the
checkbox-plus-hidden pairs for `Outstanding`, `SearchPlanning`,
`SearchEnforcement`, and `SearchAppeals`, and every text, select, and date field.
Planning stays selected while enforcement and appeals stay unselected.

The ordered qualification inventory is exactly:

1. `received:2026-08-18:2026-09-16` — 3 rows.
2. `determined:2026-08-18:2026-09-16` — 1 row, returned directly as the
   page-one detail for `PRE/1820/2026`.
3. `outstanding:planning:true` — 55 rows on six pages of
   `10, 10, 10, 10, 10, 5`.

The result pages do not publish a total or displayed row range. Completeness is
therefore proved only from observable pager facts: one current-page marker, the
complete consecutive numbered-link inventory, exact portal-provided locators,
ten rows on every page with a forward link, and no forward link on the terminal
page. The adapter never constructs a pagination URL. A full page without a
pager, a malformed current marker, shifted replay content, a mixed detail/result
shape, or a singleton after page one fails closed.

The persisted checkpoint stores the exact scope, completed-query prefix,
active-page replay proofs, and every unique human reference with its detail
locator. Resume replays already committed pages and compares their ordered
references and pager evidence before continuing. A coherent terminal checkpoint
returns before opening a network route.

## Application records and documents

Detail pages expose labelled fields for application number and type, case
officer, received and valid dates, status, proposal, location, decision fields,
applicant and agent, and plural district, electoral-division, and parish labels.
A dash in an optional date is retained as no date.

Associated documents are already present in the returned HTML behind the
`PlanningdocTable` marker and `document-list` table. Their `/Document/Download`
links expose module, record number, plan identifier, image
identifier, plan flag, and filename metadata. The qualification retained 1,368
current document metadata rows across 25 applications. The other 31 detail
responses did not expose a document section and are recorded as unavailable,
not empty, so they cannot overwrite previously known documents. The run made
zero attachment body requests. Public comments and consultee responses remain
represented only as published document attachments, so their text is explicitly
unavailable in the common comments section.

## Live qualification receipt

The durable receipt is
`.yimby/qualification-devon-2026-09-16/devon-qualification-v1.json` with SHA-256
`5a9fab5eb5802d60a099c5c646db43210bc4ec8d4716be081d2c4f78c8628dbd`.
It records:

- 56 unique discovered references and 56 persisted applications;
- 56 native and application versions, plus 25 complete document-section
  versions and 31 explicitly unavailable document sections;
- zero pending retries, failed current sections, unmapped records, comment
  versions, and attachment body requests;
- SQLite integrity and complete per-observation, registry, and
  content-addressed evidence reconciliation passing;
- 68 official requests and 6,648,256 transferred bytes on the first pass;
- an immediate terminal rerun with 0 requests, 0 bytes, and 0 attachment bodies;
- two succeeded run statuses for the qualification attempt; and
- byte-for-byte receipt preservation across a separate resumed command that also
  performed no network I/O.

All twelve named receipt checks pass. Weekly cycles due 23 and 30 September 2026
are truthfully recorded as `pending`. The adapter and bootstrap are live
collection verified for this scope, but operational qualification and
`LIVE_READY` promotion remain prohibited until those genuinely later cycles
succeed.
