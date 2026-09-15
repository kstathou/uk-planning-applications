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

