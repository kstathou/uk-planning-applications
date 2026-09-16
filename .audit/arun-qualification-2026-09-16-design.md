# Arun live qualification design

## Decision

Keep the public `Collector` and `AuthorityPackage` interfaces unchanged. Put one
canonical query plan and one discriminated progress state inside the Arun
adapter. Use exact received-date partitions for older open applications instead
of parish partitions.

The live plan for the inclusive 2026-08-18 to 2026-09-16 scope is:

1. one received-date query for the scope;
2. one decided-date query for the scope;
3. one undecided received-date query for 1948-01-01 to 1999-12-31;
4. one undecided received-date query for each calendar year from 2000 through
   2023; and
5. one undecided received-date query for each calendar month from 2024-01-01
   through the scope end, with the last window clipped to the scope end.

This produces 60 ordered queries. The 58 older-open date partitions are
mutually non-overlapping. In the canonical snapshot, received overlaps decided
by 7 references and older-open by 81; decided and older-open are disjoint. All
populations are reconciled by exact application reference. The
official portal returns an explicit empty result for the 1948-1999 open
partition. Every later partition must expose fewer than 200 rows and, where the
source reports a total, exact agreement between that total and the enumerated
reference set. A cap, unexpected pagination, missing or query-changing Show All
form, or count disagreement fails closed.

Parish partitioning was rejected. The form exposes 34 official area values, but
the portal does not state that every application has one of those values. Date
partitioning covers the same search population without that assumption.

## Types and transitions

The checkpoint separates fixture and live cursors. The live cursor owns a typed
scope, the canonical plan, a strict prefix of completed query summaries, the
global unique reference union, and exactly one of these states:

- `ready`: the next query can start;
- `show-all`: the active query's reported count and first-page references are
  retained for exact replay; or
- `complete`: every planned query is summarized and no active query remains.

Illegal combinations cannot be constructed. A terminal cursor yields one empty
complete batch without a request. A crash before the store commit safely
repeats the portal request; a crash after it resumes at the committed state.
An in-progress cursor rejects a different scope. Once its scope is complete, a
different weekly scope starts a fresh canonical plan instead of being blocked
by the prior terminal cursor.
The earlier V1 checkpoint shape is decoded explicitly. Its fixture cursor is
upconverted directly; its live cursor restarts the canonical plan for the same
scope, relying on durable reference de-duplication rather than inventing missing
per-query evidence.

The canonical plan is also the source for checkpoint validation and the receipt
query inventory. Completed query summaries retain the query key, exact
reference membership, optional source-reported count, enumerated count, and
initial and expanded result-evidence digests. They also retain the exact POST
URL, method, and ordered form fields expected for each capture. Qualification
reconstructs that contract from the retained search/result forms and rejects a
stored mismatch. Legacy terminal checkpoints without those additive fields are
still qualified by replaying retained result evidence and serialising the
reconstructed contract. These reconstructed fields prove deterministic request
construction, not which historical transport envelope produced a response. The
final receipt retains the full typed inventory and all exact durable reference
sets rather than only aggregate counts or a digest.

## Portal form boundary

The search-form parser returns a typed value, not a BeautifulSoup node. It
requires the official POST action and the exact query controls. Initial requests
preserve portal-owned hidden fields and use the captured `action=Search` submit
control. A Show All request comes only from the result-owned form and preserves
its exact query fields and `showall=showall` control.

Result parsing accepts exactly one same-host planning-result link from each
validated four-cell result row and rejects matching links outside that table.
It recognizes the portal's explicit empty structure, its `First N results
shown, there are M in total` partial count, and the source-owned complete
page whose exact four-column result table is paired with its `Back to Search
page` control. A complete page may contain multiple rows and no numeric total;
the receipt keeps its source-reported count null while retaining the exact
enumerated membership. A partial page is valid only when `N` equals the parsed
row count, `N < M`, and the exact Show All form is present. It never manufactures
a reported count from link count.
Counts or enumerations at or above 200 fail closed.

## Application and document boundary

Detail parsing requires the published reference to match the requested
reference. It also requires exactly one same-host document form whose action is
`showDocuments?reference=<reference>&module=pl` and whose submit control is
`ViewDocuments=View Documents`.

The document-index parser accepts the official unpaginated table or an explicit
empty state. Each native document retains type, published date, optional
description, primary URL, and non-empty source links. The live snapshot retains
the detail and document-index evidence captures. Attachment URLs are data only;
no attachment body is requested. Comments remain truthfully unavailable because
the official detail route does not expose a bounded text collection contract.
The official empty state requires the `Documents` heading and exact one-cell
empty table. A trailing appeal block is parsed independently so appeal `Type`
cannot become application type; its identifier/status/dates are preserved and
normalised as a relationship and dated events.
The added native document and appeal fields remain optional in
`ArunApplicationV1`, so retained payloads written before this qualification
still decode and rebuild without a schema-name fiction.

## Qualification receipt

The command writes only after all checks pass and replaces its typed,
schema-version-3 receipt atomically. The receipt proves:

- the exact inclusive scope and exact 60-query inventory;
- a terminal checkpoint whose scope-local seen-reference set is a subset of the
  cumulative durable discovery set;
- exact equality among discovered, retained-native, and materialized application
  reference sets;
- zero retries, failed current sections, unmapped records, and attachment-body
  requests;
- database integrity and recomputed application and search-evidence-digest
  integrity, including reparsing every retained result capture against its
  recorded query membership;
- exact checkpoint/source-count equality, including null source totals, and
  agreement with each reconstructed expected request contract, plus exact
  agreement between the complete native and normalised persisted models and
  their retained detail and document-index evidence, including persisted source
  identity and locator, with source identity and locator derived independently
  from retained search-result evidence;
- complete current application and document sections for every retained record;
- a successful completed run plus an immediate successful rerun with a
  byte-for-byte semantic fingerprint match; all historical run outcomes and
  aggregate bootstrap costs remain visible; and
- zero requests, zero transferred bytes, and zero attachment-body requests on
  the immediate terminal rerun; and
- the exact code revision plus persisted run identifiers, timestamps, and costs
  for the receipt publication pass and immediate follow-up.

The native coverage summary and run provenance are optional additive
schema-version-3 fields: older v3 receipts still decode, while newly published
receipts always populate them and validate them against retained evidence and
SQLite run records.

The exact 648-record portal population includes the source-published test/dummy
references `DUMMY_P`, `H/1/18/PL`, and `H/5/26/PL`. They remain in the source
inventory as ordinary unsuppressed stored application rows and are identified
explicitly as source test/dummy records in the documentation. The receipt's
exact reference inventory includes them without adding a separate classifier.

Two weekly refresh targets remain explicitly pending. The registry stays
`DISCOVERY_ONLY`; a successful bootstrap receipt does not promote Arun to
`LIVE_READY` or claim operational qualification.

## Test seams

Tests cover canonical plan construction and scope clipping, exact form payloads,
Show All replay, explicit multi-row completion, count/cap failure,
mid-query resume, terminal zero-transport behavior, exact same-host routing,
reference identity, document metadata without attachment fetches, receipt set
agreement, complete native and normalised application evidence, duplicate
detail labels, exact document-filter ownership, legacy checkpoint qualification,
search-evidence tampering, pending cycles, atomic replacement, and rerun
semantic stability.
The regression matrix also covers a second qualification scope over cumulative
SQLite state, coherent persisted source/locator tampering, missing registered
evidence rows, strict partial-count contradictions, and failed-run attachment
accounting when the transport strips query strings and a URL repeats.

## Architecture arena

Two isolated candidates were retried once after both initial lanes produced no
artifact. Candidate 1's discriminated state machine won the independent judge
28-27 and is the base. Candidate 2 supplied compact completed-query summaries,
non-empty document source links, semantic fingerprinting, and atomic receipt
replacement tests. Raw parser-object handoffs, optional contradictory live
fields, duplicated per-query reference sets, and a shared qualification
framework without a second caller were rejected.
