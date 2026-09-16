# Haringey portal walkthrough

Observed on 15 September 2026.

## Source

- Portal: `https://londonboroughofharingey.my.site.com/pr/s/`
- Shape: Salesforce Experience Cloud with Arcus public-register components
- Recorded application: `HGY/2026/2582`, Salesforce record `a0iP200000J0F49IAF`

## Recent discovery

The initial page rendered a register selector. Opening it produced quick links for applications validated in the last seven days, major applications, and a user-defined weekly list, plus simple and advanced search.

The seven-day quick link encoded its request as the `c__q` URL parameter and reported 72 results across eight pages, with 10 records on the first page. Each result exposed a Salesforce record identifier, planning reference, address, proposal, valid date, and application status. The interface also offered a table view.

## Application detail

The detail page for `HGY/2026/2582` exposed the reference, application type, address, proposal, status, officer, determination level, ward, applicant, agent, valid date, consultation end date, target decision date, Planning Portal reference, and a GIS-constraints link. Appeals and consultees were collapsed sections.

The page divided child data into details, comments, and files. The comments tab successfully loaded and stated `There are no comments.` It also warned that comments from the legacy system appear in files, while comments made in the current system appear in the comments section.

The files tab exposed six metadata rows. Each row included a date, title, type and size in the accessible link description, and a Salesforce download URL. The page also offered filters, extra detail, and `Download all`. No file or archive body was opened.

## Collection consequences

- Capture the JavaScript-populated component data or the underlying Salesforce requests. The initial HTML shell is not an empty register.
- Persist the Arcus register name, quick-link name, encoded query, page number, and Salesforce record identifier in the native model.
- Exhaust all eight result pages and reconcile the reported count.
- Keep current-system comments separate from legacy comments published as files.
- Mark comments empty only after the comment tab loads and reports no comments.
- Retain file date, title, media type, size, and source link as metadata. Never invoke `Download all` or retrieve file bodies.

## Verification status

`VERIFIED` for seven-day discovery, its 72-record pagination contract, one detail record, one explicit empty comment section, and the complete six-row file index. Advanced search, user-defined weeks, collapsed appeals and consultees, historical comments, and incremental refresh remain open.

## Browser request contract capture

The seven-day journey was rechecked on 16 September 2026. Opening the register
and selecting the quick link navigated to `/pr/s/register-view` with
`c__r=Arcus_BE_Public_Register` and a base64-encoded `c__q` payload naming the
register, `quick-link` search type, display label, and
`Planning_Applications_Weekly_List` search. The response again reported 72
records across eight pages.

Each result link carried a Salesforce record identifier in
`/pr/s/detail/<record-id>?c__r=Arcus_BE_Public_Register`. The pagination links
had no page URL; their component-local `data-id` selected the page, so the live
browser adapter must drive or reproduce that client interaction and reconcile
all 72 rows.

The detail component rendered labelled definition lists for the core fields and
separate Details, Comments, and Files tabs. The observed Comments tab explicitly
reported no comments. The Files table exposed six rows with date, title, media
type, size, and a Salesforce source link. No file link or `Download all` control
was opened. This contract requires an authority-owned page object; a single
rendered-page GET cannot prove pagination or child-section completeness.
