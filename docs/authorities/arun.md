# Arun portal walkthrough

Walkthrough date: 15 September 2026.

## Source

- OcellaWeb: `https://www1.arun.gov.uk/aplanning/OcellaWeb/`

The planning search posts reference, location, postcode, parish, applicant, agent, undecided status, application type, received dates, and decided dates.

## Application record

The observed record was `BR/156/25/PL`. The detail page exposed status, proposal, location, parish, case officer, received date, validated date, target date, comment deadline, decision date, applicant, and agent.

The page posted to separate routes for documents, other applications on the site, and comments. The comments action was disabled for the decided record.

## Documents

The document index exposed the type, date, optional description, and attachment URL. The observed types included decision, officer report, application, CIL form, plans, statement, consultation, representation letters, and system correspondence.

The walkthrough read the index and did not open any attachment. Representation text was available only through a PDF link, so the normalised comment must use the `pdf-only-unavailable` state.

## Completeness rules

The document index has a type filter but displayed every row in one table for the observed record. The scraper must enumerate the unfiltered table and retain the native type.

The application reference contains slashes and must be URL encoded. The detail page, document route, site-history route, and comment route do not use one shared parameter shape.

## Known limits

This walkthrough used a known decided reference. It did not run a bounded date search, inspect result pagination, enumerate an undecided comment flow, or test incremental changes.

## Request contract capture

The planning search was rechecked on 16 September 2026 at
`/aplanning/OcellaWeb/planningSearch`; the bare `/OcellaWeb/` path returned 404
and is not a collection route. The form posts to the current search URL. Its
fields are `reference`, `location`, `OcellaPlanningSearch.postcode`, `area`,
`applicant`, `agent`, `undecided`, `type`, `receivedFrom`, `receivedTo`,
`decidedFrom`, and `decidedTo`. Dates use `DD-MM-YY`.

A received-date search from 16 August through 16 September 2026 reported 92
records. The first response deliberately displayed only 20 and offered a
separate `Show all results` POST containing the prior search fields plus
`showall`. Submitting it exposed all 92 detail links. Each result row contained
the reference, location, proposal, and native status, and detail links used
`planningDetails?reference=...&from=planningSearch`.

This closes the previously open bounded-search and result-cap investigation for
the observed received-date path. Decided searches, window splitting at larger
caps, comment flows, and incremental changes remain open.

## Implemented boundary

The authority adapter now reproduces the captured received-date POST and its
source-provided Show All expansion. It de-duplicates references, stores the
detail locator, and rejects older-open or unsupported window shapes before
claiming completeness. The visible detail fields are parsed from the recorded
route. The document action remains unavailable because its exact POST
parameters were not captured, and no attachment body is requested.
