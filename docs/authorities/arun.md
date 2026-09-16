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

The result parser reconciles a source-reported partial total with the exact
expanded references, rejects duplicates within a response, and de-duplicates
overlaps across received, decided, and older-open searches by application
reference. Some complete portal responses publish no numeric total. Those are
accepted only when the page has the exact four-column result table and its
source-owned `Back to Search page` control; every row is enumerated, but the
source-reported count remains null. It never labels a link count as a reported
count. Each membership link must occur in exactly one validated result-table
row; matching links outside the table fail closed. All search form and result
HTML used for the qualification is retained by digest, and the receipt reparses
it against the exact per-query reference membership and checkpoint count.

## Application and document records

The detail page exposes the reference, native status, proposal, location,
optional parish, officer, received and validated dates, decision-by, target
committee, comment-by and decision dates, decision, applicant, and agent. The
application reference contains slashes and is URL encoded when routed.
The trailing appeal block is parsed separately: its `Appeal`, `Lodged`, `Type`,
and `Decision` values become a native appeal identifier, lodged date, appeal
status, and appeal decision date. Appeal `Type` is not an application type.

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
received rows, 118 decided rows, and 529 rows across the 58 non-overlapping
older-open partitions into 648 unique references. In this snapshot, received
overlaps decided by 7 references and older-open by 81; decided and older-open
are disjoint. All 648 applications were materialised with two evidence captures
each (detail and document index), for 1,296 verified content digests.

The completed schema-version-3 qualification accounts for 1,373 official-page
requests and 11,499,963 transferred bytes across the whole bootstrap history.
That total deliberately includes two fail-closed development attempts whose
stable `ArunParseError` diagnostics remain in SQLite; neither attempt published
an incomplete receipt. The network-bearing completion resume made 963 requests
and transferred 8,536,426 bytes. After a zero-transport offline normaliser
rebuild, the schema-version-3 receipt publication pass and its immediate rerun
each made zero requests and transferred zero bytes. The completed snapshot has
zero pending retries, failed current sections, unmapped records, and
attachment-body requests.

The 648 native rows retain 648 received dates, 395 validated dates, 396
decision-by dates, 323 comment-by dates, 38 target-committee dates, 119 decision
dates, and 648 native decision statuses. Decision-by, comment-by, and
target-committee values are also normalised as 396 `decision-due`, 323
`comment-deadline`, and 38 `target-committee` events with detail-page
provenance. Retained detail evidence also proves 4 appeal identifiers, 3 appeal
statuses, 2 appeal-lodged dates, and 3 appeal-decision dates. These are
normalised as 4 appeal relationships, 2 `appeal-lodged` events, and 3
`appeal-decision` events. All 648 current application sections use the
`arun-v5` normaliser;
the Unicode whitespace in the 253 `Undecided (On Hold)` source statuses is
collapsed to the stable `undecided-(on-hold)` value. The receipt records 73 search captures and 1,296 application
captures with their exact digests. It reparses every result capture, checks its
exact query membership, and also verifies SQLite
integrity, evidence paths, terminal checkpoint state, exact reference-set
equality, current section completeness, the source cap, and the durable
discovery-only registry status before publication. The 22 checks include exact
agreement between every reparsed native appeal/application-type field and its
retained detail evidence, and between native document metadata and the retained
document index. The immediate terminal rerun made zero requests,
transferred zero bytes, requested zero attachment bodies, and produced the same
durable snapshot and semantic fingerprint. Zero-request successes do not advance
the source-freshness timestamp; it remains the last source-bearing completion at
2026-09-16 12:06:26 UTC.

The canonical local receipt is
`.yimby/qualification-arun-2026-09-16/arun-qualification-v3.json`. It contains
the typed full query inventory, the optional source-reported count, exact
enumerated count and references for every query, exact durable reference sets,
separate search and application evidence digest inventories, Arun-scoped costs
and run outcomes, check results, and pending weekly-cycle dates. The superseded
v2 receipt remains beside it for audit history, and the pre-v2 snapshot is preserved at
`.yimby/qualification-arun-2026-09-16-pre-v2` for diagnosis only.

## Remaining operational limit

This proves the live bootstrap only. Refreshes targeted for 23 and 30
September 2026 have not happened, so Arun remains `discovery-only` and must not
be described as operationally qualified or `LIVE_READY`. Same-day reruns do
not substitute for those two later weekly cycles.
