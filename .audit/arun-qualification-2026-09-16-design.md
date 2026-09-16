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

This produces 60 non-overlapping, ordered queries. The official portal returns
an explicit empty result for the 1948-1999 open partition. Every later partition
must expose fewer than 200 results and exact agreement between its reported and
enumerated reference sets. A cap, unexpected pagination, missing Show All form,
or count disagreement fails closed.

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

The canonical plan is also the source for checkpoint validation and the receipt
query inventory. Completed query summaries retain the query key, reported count,
and enumerated count. The final receipt retains the full typed inventory rather
than only a digest.

## Portal form boundary

The search-form parser returns a typed value, not a BeautifulSoup node. It
requires the official POST action and the exact query controls. Initial requests
preserve portal-owned hidden fields and use the captured `action=Search` submit
control. A Show All request comes only from the result-owned form and preserves
its exact query fields and `showall=showall` control.

Result parsing accepts only references from planning-result links. It recognizes
the portal's explicit empty message and its `First 20 results shown, there are N
in total` count. A query is complete only when the number of unique references
equals the reported total. Counts at or above 200 fail closed.

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

## Qualification receipt

The command writes only after all checks pass and replaces its versioned receipt
atomically. The receipt proves:

- the exact inclusive scope and exact 60-query inventory;
- a terminal checkpoint whose seen-reference set equals the durable discovery
  set;
- exact equality among discovered, retained-native, and materialized application
  reference sets;
- zero retries, failed current sections, unmapped records, and attachment-body
  requests;
- database integrity and recomputed evidence-digest integrity;
- complete current application and document sections for every retained record;
- two successful runs with a byte-for-byte semantic fingerprint match; and
- zero requests, zero transferred bytes, and zero attachment-body requests on
  the immediate terminal rerun.

Two weekly refresh targets remain explicitly pending. The registry stays
`DISCOVERY_ONLY`; a successful bootstrap receipt does not promote Arun to
`LIVE_READY` or claim operational qualification.

## Test seams

Tests cover canonical plan construction, exact form payloads, Show All replay,
count/cap failure, mid-query resume, terminal zero-I/O behavior, reference
identity, document metadata without attachment fetches, receipt set agreement,
evidence tampering, pending cycles, atomic replacement, and rerun semantic
stability.

## Architecture arena

Two isolated candidates were retried once after both initial lanes produced no
artifact. Candidate 1's discriminated state machine won the independent judge
28-27 and is the base. Candidate 2 supplied compact completed-query summaries,
non-empty document source links, semantic fingerprinting, and atomic receipt
replacement tests. Raw parser-object handoffs, optional contradictory live
fields, duplicated per-query reference sets, and a shared qualification
framework without a second caller were rejected.
