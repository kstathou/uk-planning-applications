# Arun portal walkthrough

Walkthrough dates: 15 and 16 September 2026.

## Official source and request contract

The collector uses only Arun District Council's OcellaWeb planning search at
`https://www1.arun.gov.uk/aplanning/OcellaWeb/planningSearch`. The bare
`/OcellaWeb/` path returned 404 and is not a collection route.

The search form posts to the same route. Its controls are `reference`,
`location`, `OcellaPlanningSearch.postcode`, `area`, `applicant`, `agent`,
`undecided`, `type`, `receivedFrom`, `receivedTo`, `decidedFrom`, and
`decidedTo`; dates use `DD-MM-YY`. A capped first response offers a
portal-owned `Show all results` POST containing the exact active search fields
plus `showall`. The adapter rejects missing, duplicated, cross-host, or
query-changing Show All forms, and rejects any reported count of 200 or more
rather than claiming completeness.

## Discovery boundary

The canonical live plan contains exactly 60 searches:

- one received-date search and one decided-date search for the inclusive
  30-day window;
- one older-open received-date search for 1948 through 1999;
- one older-open received-date search per year from 2000 through 2023; and
- one older-open received-date search per month from January 2024 through the
  clipped end month.

Older-open searches set the portal's undecided control and use received dates.
This avoids assuming that the optional parish field covers the whole council.
The large 1948--1999 partition returned the portal's explicit empty result;
every other partition must remain below the 200-result cap. Completed queries
are immutable in the versioned checkpoint. An interrupted active query is
replayed before Show All so that a newly arrived first-page record is included
without repeating completed partitions.

The result parser reconciles the reported total with the exact enumerated
references, rejects duplicates within a response, and de-duplicates overlaps
across received, decided, and older-open searches by application reference.

## Application and document records

The detail page exposes the reference, native status, proposal, location,
optional parish, officer, received and validated dates, target and comment
dates, decision, applicant, and agent. The application reference contains
slashes and is URL encoded when routed.

The exact document action posts to
`showDocuments?reference=...&module=pl`. The index is an official headerless
five-column table and exposes document type, optional date, optional
description, and a `viewDocument` source link. The collector retains that
metadata and link but never requests an attachment body. The exact phrase
`There are no documents for this section` is recorded as an empty document
section; unknown or ambiguous shapes fail closed.

Public comment text was not available through a proven route during this
qualification, so comments remain explicitly unavailable. No PDF or other
attachment was opened to infer comment text.

## Live bootstrap qualification

The persisted qualification on 16 September 2026 used the inclusive window
18 August through 16 September and the exact 60-query plan. It reconciled 89
received rows, 118 decided rows, and 529 rows across the overlapping older-open
partitions into 648 unique references. All 648 applications were materialised
with two evidence captures each (detail and document index), for 1,296 verified
content digests.

The first pass made 1,329 official-page requests and transferred 11,248,979
bytes. It recorded zero pending retries, failed current sections, unmapped
records, and attachment-body requests. SQLite integrity, evidence paths and
digests, terminal checkpoint state, exact reference-set equality, current
section completeness, and the source cap were checked before the versioned
receipt was atomically published. The immediate terminal rerun made zero
requests, transferred zero bytes, and produced the same durable snapshot and
semantic fingerprint.

The local receipt is
`.yimby/qualification-arun-2026-09-16/arun-qualification-v1.json`. It contains
the full query inventory, exact reference sets, counts, evidence digest
inventory, costs, checks, and pending weekly-cycle dates.

## Remaining operational limit

This proves the live bootstrap only. Refreshes targeted for 23 and 30
September 2026 have not happened, so Arun remains `discovery-only` and must not
be described as operationally qualified or `LIVE_READY`. Same-day reruns do
not substitute for those two later weekly cycles.
