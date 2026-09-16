# Cheshire East portal walkthrough

Observed on 15 and 16 September 2026.

## Source

- Portal: `https://pa.cheshireeast.gov.uk/planning/index.html?fa=search`
- Shape: custom council register
- Separate weekly route: `?fa=getReceivedWeeklyList`
- Direct detail route: `?fa=getApplication&id=<numeric locator>`

The older guessed IDOX route is not a source. It returned HTTP 404 during the
portal census.

## API-first source census

The source census was repeated on 16 September 2026 before returning to the
HTML register. The council's open-data page identifies Insight Cheshire East as
its official data portal. Its ArcGIS organisation is
`APHjSHuFMGWVZFgQ`. The exact ArcGIS catalogue query was
`orgid:APHjSHuFMGWVZFgQ AND ("planning application" OR "weekly list")` at
`https://www.arcgis.com/sharing/rest/search?f=json&num=100&q=orgid%3AAPHjSHuFMGWVZFgQ%20AND%20%28%22planning%20application%22%20OR%20%22weekly%20list%22%29`.
It returned no planning-application or weekly-list item, and
`https://services3.arcgis.com/APHjSHuFMGWVZFgQ/ArcGIS/rest/services` exposed no
planning-application service. The national Planning Data organisation page at
`https://provide.planning.data.gov.uk/organisations/local-authority%3ACHE`
likewise exposed no Cheshire East application feed or endpoint. The available
planning-related datasets were policy, boundary, brownfield, or aggregate
datasets rather than the statutory application register.

The portal's interactive-map page at
`https://pa.cheshireeast.gov.uk/planning/index.html?fa=search_map` did not
provide an alternative application inventory. Its public script at
`https://pa.cheshireeast.gov.uk/gis/systems/gb-council-direct/gis.js` fell back
to the generic `planning_demo` WFS namespace because the page published no
council-specific GIS environment. The advertised
`https://pa.cheshireeast.gov.uk/gis/ajax.html?fa=getAvailableLayers` route
redirected to login. The script's zero-feature application request, reproduced
with its `planning_demo` fallback, was:

```text
https://geoserver.tascomi.com/geoserver/planning_demo/ows?typenames=planning_demo%3Aapplications&service=WFS&request=GetFeature&maxFeatures=0&outputFormat=application%2Fjson&LAYERS=planning_demo%3Aapplications&VERSION=1.1.1
```

That exact request returned HTTP 523. It published no Cheshire East namespace,
freshness fact, total, or terminal boundary that could support qualification.

Export and dump discovery followed the complete official publication chain:
the council open-data page, the organisation-wide ArcGIS item query across all
item types, the ArcGIS service directory, and the Planning Data organisation
overview. None exposed a planning-application item, file, service, download, or
dump, so there was no item-level CSV, GeoJSON, or bulk-download URL to qualify.
These API, open-data, GIS, export, and dump candidates were therefore rejected
before the HTML register was considered.

Those API-first results are observations from the exact named requests and
discovery method, not content-addressed qualification evidence: their response
bodies were not added to the evidence store. They therefore constrain source
selection but do not prove a live inventory or support promotion.

## Search contract

The search form is named `form`, uses `POST`, and submits to
`/planning/index.html`. Dates use `DD-MM-YYYY`. Its successful controls include:

- hidden `fa` and `submitted`
- `application_reference_number`, `application_type_id`, `proposal`, and
  `decision_type_id`
- `Applicant[applicant_name]`, `Applicant[company_name]`,
  `Agent[agent_name]`, and `Agent[company_name]`
- `ps_development_code_id`, `SiteAddress[magic]`, `SiteAddress[postcode]`,
  `SiteAddress[Street][street_description]`, and `site_address_description`
- `site_address_x`, `site_address_y`, `ward_id`, and `community_id`
- `valid_date_from`, `valid_date_to`, `received_date_from`,
  `received_date_to`, `committee_proposed_date_from`,
  `committee_proposed_date_to`, `decision_issued_date_from`, and
  `decision_issued_date_to`

The current browser form was observed with empty hidden `fa` and `submitted`
values; an earlier portal response used `fa=search`. The implementation replays
either accepted discriminator shape in source order. It sets both valid-date
bounds, blanks every documented filter including hidden address coordinates,
omits checkbox and radio filters, and rejects unknown successful controls. A
single-select filter must expose one enabled blank option. Browser-effective
disabled fieldsets, form ownership, encoding, and option disabledness are
validated before submission. The qualification scope was the inclusive
30-day range 18 August through 16 September 2026.

The latest browser recheck returned exactly 30 rows for that valid-date request,
including `26/3335/PRIOR-1A`, and exactly 30 rows for the unbounded visible
`Not Determined` decision filter. Neither response published a result total,
pagination control, continuation token, or all-results-loaded marker. The
earlier explicit-zero response was observed, but its body was not retained and
it is no longer treated as the current result. Neither current response proves
a complete enumeration of recent or active records.

The parser treats only the rendered `strong.text-danger` value `No Results
Found.` inside the unique `div.application-list > div.push-30-t` result
boundary as terminal for that zero response. Hidden, duplicated, unscoped, or
mixed result boundaries fail closed, including non-rendering ancestors,
Bootstrap hide and closed-collapse states, closed native containers, unopened
popovers, and native fallback content. The same rendered-boundary check rejects
ambiguous duplicate inline declarations, CSS comments, and CSS escapes, and
applies to a positive table. A positive result table publishes no result total,
pagination boundary, or all-results-loaded marker, so a non-empty page remains
explicitly unproved rather than being treated as the complete 30-day inventory.
The current positive shape has nine exact columns: application reference,
application type, location details, proposal, ward, community, consultation
closes, decision, and view. Its numeric `data-id` remains the stable detail
locator.

The register states that appeals are not visible and points users to the
Planning Inspectorate.

## Weekly boundary

`GET /planning/index.html?fa=getReceivedWeeklyList` exposes a `POST` form on the
same route. Its exact successful fields are `week=DD-MM-YYYY` and hidden `fa`.
The page normalises a selected date to Monday.

The current default list displayed four rows. A request for the week beginning
1 January 2024 displayed exactly 50 rows. That historical response published
no total, next-page link, or all-results-loaded marker. There is no source fact
that distinguishes a complete 50-row week from a truncated response, so this
route cannot prove an exhaustive historical partition or all older active
applications. Generic button or status text is not accepted as a terminal
marker because no exact source marker has been verified.

## Detail and documents

The browser opened numeric locator `406569` at
`/planning/index.html?fa=getApplication&id=406569` and verified public
reference `26/3335/PRIOR-1A`. The page exposed application type, proposal,
applicant, agent, location, British National Grid coordinates, ward, parish,
officer, decision level, application status, received date, valid date, expiry
date, and consultation-end date.

`table#application_documents` displayed five rows with document type,
description, thumbnail metadata, date added, and one
`?fa=downloadDocument&id=<document id>&public_record_id=406569` link per row.
The disabled `#all_documents_loaded_application_documents` control was present
and the show-more control was hidden. No thumbnail or attachment body was
requested.

The parser verifies the numeric locator and public reference before accepting
the detail. It accepts document metadata only when exactly one document
section contains exactly one table, disabled all-loaded marker, and hidden
show-more control with the exact labels and table columns. The section, table,
and all-loaded marker must be rendered, while the show-more control must have
the verified hidden state. Duplicate or out-of-section controls fail closed.
Attachment URLs remain metadata.

## Automated qualification result

The automated HTTP transport could not reproduce the browser search form on
16 September 2026. One direct HTTP check returned an AWS WAF challenge header;
the qualification run retained a 2,019-byte HTML response that did not contain
the recorded form. A generic local headless browser reached an `IDX005` error
document. The successful interactive browser surface is not available to the
collector as a stable transport.

The qualification command made one official source request, retained its
content-addressed body, requested no attachment bodies, created no SQLite
store, and wrote a typed v2 blocked receipt. The receipt preserves the exact
attempted GET URL independently of the transport-sanitised evidence URL. Its
immediate `--resume` rerun read only the receipt and evidence. The intended
recent, weekly-form, historical-week, and direct-detail requests are recorded
as pending rather than falsely reported as run. Any later parser drift also
records the decision stage while retaining every completed request, response,
and cumulative byte cost in an offline-resumable typed blocker. Resume reparses
each retained form, every complete result and weekly row, the detail including
finite British National Grid coordinates, and each document-metadata row, then
requires the reconstructed contract to equal the receipt. Evidence files and
their newly created directory chain are synced before a completed journal stage
is published. A crash before initial journal publication can recover under the
process lock. Blocker explanations are fixed by their typed codes. `--resume`
without a receipt or durable journal fails during configuration before a portal
session is constructed.

## Verification status

`OBSERVED` for the interactive-browser form inventory, weekly form, direct
detail route, reference match, and five-row document metadata table. Those
browser response bodies were not added to the content-addressed evidence store;
the observed shapes are encoded as regression fixtures, not presented as
retained qualification proof.

`VERIFIED` for the retained automated blocker receipt: one 2,019-byte WAF
response, zero attachment requests, no SQLite store, and zero source requests
on immediate offline resume.

`BLOCKED` for live collection. The current recent and not-determined searches
both stop at exactly 30 rows without terminality; the historical weekly list
stops at exactly 50 rows without terminality; older-open completeness is
unproved; and the automated transport receives an AWS WAF JavaScript challenge
instead of the recorded search form. The authority is not `LIVE_READY`. Its two
operational weekly cycles due on 23 and 30 September 2026 remain pending.
