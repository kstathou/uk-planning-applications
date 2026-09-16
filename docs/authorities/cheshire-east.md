# Cheshire East portal walkthrough

Observed on 15 and 16 September 2026.

## Source

- Portal: `https://pa.cheshireeast.gov.uk/planning/index.html?fa=search`
- Shape: custom council register
- Separate weekly route: `?fa=getReceivedWeeklyList`
- Direct detail route: `?fa=getApplication&id=<numeric locator>`

The older guessed IDOX route is not a source. It returned HTTP 404 during the
portal census.

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

The implementation serialises the successful controls in source order and
overrides both valid-date bounds for an exact request. Browser-effective
disabled fieldsets, including the first-legend exception, are applied before a
control can be validated or submitted. The qualification scope was the
inclusive 30-day range 18 August through 16 September 2026.

The browser returned an explicit no-results response for that valid-date
request. It also returned no results when `decision_type_id` was set to the
visible `Not Determined` value. Both responses contradict direct official
detail `26/3335/PRIOR-1A`, whose application status is `Pending Consideration`
and whose valid date is 14 September 2026. Neither form is therefore a proven
enumeration of the requested recent or active records.

The parser treats only the rendered `strong.text-danger` value `No Results
Found.` inside the unique `div.application-list > div.push-30-t` result
boundary as terminal for that zero response. Hidden, duplicated, unscoped, or
mixed result boundaries fail closed, including non-rendering ancestors. The
same rendered-boundary check applies to a positive table. A positive result table publishes no
result total, pagination boundary, or all-results-loaded marker, so a non-empty
page remains explicitly unproved rather than being treated as the complete
30-day inventory.

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
retains every completed response in an offline-resumable typed blocker. Resume
reparses each retained form, every complete result and weekly row, the detail
including grid coordinates, and each document-metadata row, then requires the
reconstructed contract to equal the receipt. Blocker explanations are fixed by
their typed codes. `--resume` without its receipt fails during configuration
before a portal session is constructed.

## Verification status

`VERIFIED` for the browser form inventory, weekly form, direct detail route,
reference match, and complete five-row document metadata table.

`BLOCKED` for live collection. Recent-window fidelity is contradicted,
weekly-list terminality is unproved, older-open completeness is unproved, and
the automated transport cannot currently recover the recorded search form.
The authority is not `LIVE_READY`. Its two operational weekly cycles due on
23 and 30 September 2026 remain pending.
