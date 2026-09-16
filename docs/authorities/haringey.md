# Haringey portal walkthrough

Observed on 15 September 2026.

## Source

- Portal: `https://londonboroughofharingey.my.site.com/pr/s/`
- Shape: Salesforce Experience Cloud with Arcus public-register components
- Recorded application: `HGY/2026/2582`, Salesforce record `a0iP200000J0F49IAF`

## Recent discovery

The initial page rendered a register selector. Opening it produced quick links for applications validated in the last seven days, major applications, and a user-defined weekly list, plus simple and advanced search.

The seven-day quick link encoded its request as the `c__q` URL parameter. It
reported 72 results across eight pages during the first walkthrough and 59
results across six pages during the 16 September recheck. Counts are live
values and must never be hardcoded. Each result exposed a Salesforce record
identifier, planning reference, address, proposal, valid date, and application
status. The interface also offered a table view.

## Application detail

The detail page for `HGY/2026/2582` exposed the reference, application type, address, proposal, status, officer, determination level, ward, applicant, agent, valid date, consultation end date, target decision date, Planning Portal reference, and a GIS-constraints link. Appeals and consultees were collapsed sections.

The page divided child data into details, comments, and files. The comments tab successfully loaded and stated `There are no comments.` It also warned that comments from the legacy system appear in files, while comments made in the current system appear in the comments section.

The files tab exposed six metadata rows. Each row included a date, title, type and size in the accessible link description, and a Salesforce download URL. The page also offered filters, extra detail, and `Download all`. No file or archive body was opened.

## Collection consequences

- Capture the JavaScript-populated component data or the underlying Salesforce requests. The initial HTML shell is not an empty register.
- Persist the Arcus register name, quick-link name, encoded query, page number, and Salesforce record identifier in the native model.
- Exhaust every reported result page and reconcile the dynamic count.
- Keep current-system comments separate from legacy comments published as files.
- Mark comments empty only after the comment tab loads and reports no comments.
- Retain file date, title, media type, size, and source link as metadata. Never invoke `Download all` or retrieve file bodies.

## Verification status

`VERIFIED` for the dynamic seven-day discovery contract, one detail record, one
explicit empty comment section, and the complete six-row file index. Advanced
search, user-defined weeks, non-empty current comments, collapsed appeals and
consultees, historical comments, older-open enumeration, and incremental
refresh remain open.

## Browser request contract capture

The seven-day journey was rechecked on 16 September 2026. Opening the register
and selecting the quick link navigated to `/pr/s/register-view` with
`c__r=Arcus_BE_Public_Register` and a base64-encoded `c__q` payload naming the
register, `quick-link` search type, display label, and
`Planning_Applications_Weekly_List` search. The source reported 59 records
across six pages on this recheck, rather than the earlier 72 across eight.

Each result link carried a Salesforce record identifier in
`/pr/s/detail/<record-id>?c__r=Arcus_BE_Public_Register`. The pagination links
had no page URL; their component-local `data-id` selected the page, so the live
browser adapter drives that client interaction and reconciles the reported
rows.

The detail component rendered labelled definition lists for the core fields and
separate Details, Comments, and Files tabs. The observed Comments tab explicitly
reported no comments. The Files table exposed six rows with date, title, media
type, size, and a Salesforce source link. No file link or `Download all` control
was opened. This contract requires an authority-owned page object; a single
rendered-page GET cannot prove pagination or child-section completeness.

## Implemented browser boundary

The authority-owned page object opens the role button named `Haringey Public
Register`, then the exact quick-link button named `Planning Applications
Validated in last 7 days`. Result containers use `.slds-form.slds-box`; detail
links match `a[href*="/pr/s/detail/"]`; the reported range uses
`.pr-pagination__results`; and numeric pages use
`a.pr-pagination__link[data-id="<page>"]`. The link named `Nextset of pages`
advances the component-local page selector. The adapter reads the counts from
the page and rejects a mismatch between the range, visible cards, pages, or
final total.

Detail routing retains the Salesforce record identifier and source URL. The
page object waits for the heading named by the public reference, opens the
Comments and Files tabs by role, and accepts the recorded comment section only
after the exact `There are no comments.` message appears. It enumerates file
table metadata but has no operation that activates a file link or `Download
all`. Salesforce `/sfc/servlet.shepherd/version/download/` paths are blocked by
fixture, HTTP, and browser transports before a body request.

This remains `BROWSER_ONLY`, not live-ready. The public UI exposes only a
rolling seven-day quick link through this implemented path. Arbitrary date
windows, older-open enumeration, and a complete live bootstrap are not proved.

## Qualification attempt on 16 September 2026

The official register index first reported `Something went wrong` and `The
system was unable to load any registers.` at 09:42 Europe/London. A second
check at 09:47 returned `Looks like the site is temporarily unavailable` and
`Please try again in a bit.` The documented detail route for
`HGY/2026/2582` returned the same temporary-unavailability message.

The outage prevented a current walkthrough of the user-defined weekly and
advanced-search controls. It also prevented proof of their exact date
semantics, result caps, pagination, and complete older-open status inventory.
The adapter therefore remains `BROWSER_ONLY`. No live collection ran, no
qualification receipt was emitted, and both later weekly refresh cycles remain
pending.

The local blocker artifact is
`.yimby/qualification-haringey-2026-09-16/source-blocker-v1.json`. It records
the official URLs, visible messages, required checks that did not run, and the
pending refresh cycles. The artifact is evidence of a blocked attempt. It is
not a live-qualification receipt.

The source recovered later that morning. A live page-object smoke then opened
the implemented rolling-seven-day route, reported 61 records across seven
pages, retained the first ten rendered references, and made zero attachment-body
requests. This proves the existing bounded smoke boundary is healthy; it does
not prove a complete bootstrap.

The recovered user-defined weekly control states that it includes records for
seven days from the chosen date. A search from 18 August 2026 reported 61
records across seven pages: the first page contained records valid on 25 August
and the final, 61st row was valid on 18 August. The observed interval is thus
inclusive of both the selected date and selected date plus seven days. This is
sufficient evidence for overlapping, clipped queries over the requested
30-day window.

Older-open completeness remains blocked. The register home page exposes only
the validated-last-seven-days and major-application quick links plus the
user-defined weekly list. Advanced planning search exposes reference, address,
postcode, proposal, application type, ward, valid-date bounds, decision-date
bounds, and Planning Portal number, but no application-status criterion. A
broad search reported more results than could be shown and exposed only 250
rows across 25 pages. Without an official all-open route, a status inventory,
or a proved historical lower bound for date partitioning, filtering those rows
cannot prove that every older open application was enumerated. Qualification
therefore remains fail-closed: no adapter expansion, persisted bootstrap, or
qualification receipt was produced, and both genuinely later weekly cycles
remain pending.

## Current-map follow-up on 16 September 2026

The official council page headed `Map of current planning applications` says
that its embedded map shows all current planning applications in Haringey. The
map configuration names the public layer `planning_current_apps`, the WFS
endpoint `https://my.haringey.gov.uk/GetOWS.ashx`, and the feature-information
endpoint `https://my.haringey.gov.uk/MapGetImage.aspx`.

A WFS 1.1.0 `GetFeature` request with `RESULTTYPE=hits` advertised 1,445
features at 10:15:29 Europe/London. A whole-borough
`GetMultiInfoFromShape` request over bounds `522831,182787` to
`539720,195900` independently returned 1,445 feature identifiers and 1,445
unique Salesforce planning-application links. The advertised, observed, and
unique-link counts therefore reconcile exactly for this named layer. The
1,781,204-byte feature response had SHA-256 digest
`943f5fea06a34112a5b813e3392b3b3df77722ed6089ab562442a8428ade2d7d`.

The response is labelled GeoJSON but is not valid JSON: at least one proposal
contains unescaped quotation marks. Any future diagnostic parser must therefore
fail closed around the feature, field, and structured-link boundaries rather
than accepting a partial JSON parse.

This discovery does **not** remove the older-open blocker. Feature 1 resolves
to `HGY/2023/2916`, whose council detail page reports `Decision Made`, decision
date 28 November 2023, and decision `No Objection`. The layer is therefore not
an exact open-case inventory. It may be an undocumented or stale superset, but
neither the map metadata nor the register proves that every older open
application is present. The layer is useful additional discovery evidence only;
its count must not be represented as older-open completeness.

The source subsequently returned its maintenance page again. No production
adapter expansion, persisted bootstrap, qualification receipt, or readiness
promotion follows from this map investigation. The two genuinely later weekly
cycles remain pending.

## Legacy cutover-map follow-up on 16 September 2026

The map service also exposes the older `mapsources/WebTeam` configuration used
by Haringey's official lite-map client. That client defaults to
`curr_planning_apps_solo`; the same map source separately names
`decided_planning_apps_solo`. The WFS capabilities likewise separate current
and decided layer families. Together with the newer Arcus-backed
`planning_current_apps` layer, this is credible first-party evidence for an
exhaustive *discovery* boundary spanning both portal eras, provided each named
layer is reconciled independently and the counts are read live rather than
hardcoded.

At 10:51:50 Europe/London, WFS advertised 826 legacy-current features. A
whole-borough shape request independently returned 826 feature identifiers and
826 unique legacy PKIDs. Its 905,495-byte response had SHA-256 digest
`674aa270bdee2ed262be44d5933c962395a1cc912319559e53042450aee752b3`.
The newer current-layer WFS count was 1,442 at 10:52:10, down from 1,445 during
the earlier observation, confirming that these are volatile live counts.

At 11:26:57 Europe/London, the separately configured legacy-decided WFS
advertised 13,974 features. A throttled 4-by-4 whole-borough shape
reconciliation returned 13,974 unique PKIDs paired with 13,974 unique HGY
references and no ambiguous PKID. The ordered tile bodies totalled 13,023,218
bytes; framing each body with `row:column:byte-count` produced SHA-256 digest
`b4d7d2461397c9c1d119747cda731a4bc4fe0a9cba56a890c82f94ab41d9882a`.
The intersection between those 13,974 decided PKIDs and all 826
legacy-current PKIDs was exactly zero. The decided layer is therefore a
complete official PKID-to-HGY lookup for its own population, but cannot
crosswalk even one member of the cutover-current population.

The legacy-current layer is itself a stale cutover-era superset. PKID `19085`
publishes address `70 Tetherdown` and the satellite-dish proposal; an exact
register search finds `HGY/1999/0250`, now `Decision Made` and disposed on 9
January 2023. Near the other end of the layer, PKID `442757` publishes the
larger-home-extension proposal at `59 Creighton Road`; the exact matching Arcus
record is `HGY/2022/2811`, `Decision Made`, refused on 5 December 2022. These
content matches diagnose the stale snapshot but are not accepted as identity
crosswalks.

Every legacy feature links only to
`www.planningservices.haringey.gov.uk/portal/servlets/ApplicationSearchServlet?PKID=...`.
That official host no longer resolves. The map metadata exposes only address,
proposal, and the dead link; its WFS schema is geometry-only. Searching the
current Arcus register for PKID `19085` returns no result. The rendered detail,
the public record payload, and the configured detail sections expose the HGY
reference and Salesforce record ID but no legacy PKID. The only external-ID
field published in the detail contract is the Planning Portal reference, which
is empty for both sampled historic records. A guest `RecordGvp.getRecord`
request for the `FULL` layout exposed only the standard name, ID, ownership,
record-type, creation, modification, and system-timestamp fields. It exposed no
custom field at all. The guest UI-API object-info endpoint returned HTTP 403,
while `PublicRegisterViewService.getRecordDetails` exposed the configured
custom fields and still named no legacy identifier other than the empty
`arcusbuiltenv__External_Id__c` field.

The result is narrower than the earlier map conclusion: older-open discovery
now has a credible union strategy, but complete collection does not. All 826
legacy PKIDs would need an official deterministic PKID-to-HGY-to-Salesforce
crosswalk, with zero missing or ambiguous mappings, before their current
status, detail, comments, and file metadata could be fetched and reconciled.
Joining by address or proposal would be a content guess even when an exact
sample happens to be unique, and the independently reconciled decided layer
has zero PKID overlap to exploit. Qualification therefore remains fail-closed,
the adapter stays `BROWSER_ONLY`, and no receipt or weekly-cycle credit is
emitted.
