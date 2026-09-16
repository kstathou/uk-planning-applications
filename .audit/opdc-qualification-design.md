# OPDC live qualification synthesis

## Decision

Use the compact authority-owned design from the flow candidate, strengthened by
the proof candidate's typed receipt constraints. The existing collector
boundary remains unchanged. A post-implementation provenance review required
one narrow SQLite migration so each rebuild input preserves its capture URL and
media type independently of content-addressed body deduplication.

The live adapter will own:

- an inclusive discovery scope;
- the ordered `registered-window`, `determined-window`, `registered-open`
  inventory;
- canonical `(reference, locator)` identities and per-query result totals;
- full-array count validation and exact-prefix checkpoint transitions;
- application, document-metadata, and public-response parsing; and
- attachment metadata URLs that are never requested.

Shared transport gains only typed public routing headers and preflight blocking
for OPDC's extensionless attachment route. Non-HTTP sessions must reject rather
than ignore headers.

## Proof boundary

The qualification command derives the requested 30 calendar days as
2026-08-18 through 2026-09-16, requires complete current `Registered`
enumeration, and refuses a changed scope in a non-empty qualification store.
Its versioned receipt must prove:

- exact ordered query inventory and declared result totals;
- a coherent terminal checkpoint;
- full identity agreement between the checkpoint and durable discovery queue;
- one normalised application for every discovered reference;
- detail, document-metadata, and response evidence for every application;
- no retry backlog, failed current section, unmapped record, missing evidence,
  digest-invalid evidence, attachment-body attempt, or failed run;
- SQLite integrity; and
- an unchanged immediate rerun with zero requests and zero transferred bytes.

The official portal's client-side pager and `total == len(results)` establish
query exhaustion. The dated browser walkthrough establishes that the unbounded
`status=registered` query is the portal's complete current registered/open
surface. Two later weekly cycles remain separate acceptance work.

## Rejected scope

Discovery-body persistence, a new checkpoint-version framework, and a generic
qualification framework were rejected as broader than this authority requires.
Search proof remains a validated checkpoint and typed receipt; retained detail,
document-index, and response captures remain the durable source evidence.

## Arena record

- Candidate 1: `/private/tmp/opdc-architect/candidate-flow.md`
- Candidate 2: `/private/tmp/opdc-architect/candidate-proof.md`
- Cross-judge: preferred candidate 1, grafted typed zero-cost receipt proofs and
  full identity agreement, and rejected the new discovery-evidence schema.
- Agent isolation: candidates wrote separate temporary artifacts; the judge was
  read-only; only the parent edits repository files.
- Runtime note: the requested custom architect profile was unavailable, so the
  generic-agent fallback was used; agent model identities are unverified and
  all conclusions require parent tests and live verification.

## Post-review correction

The first live receipt was rejected after independent review. Content-addressed
bodies were stored safely, but rebuild inputs retained only digests, so identical
document or response bodies could rehydrate the URL of the first application
that stored that digest. A digest-equality exception in the qualification check
masked that loss of provenance. The rejected dataset remains recoverable as
`.yimby/qualification-opdc-2026-09-16.invalid-pre-evidence-fix/`.

Migration 006 now preserves the ordered `(digest, source URL, media type)`
association for each rebuild input while continuing to store each body once.
Evidence reads recompute SHA-256 after decompression, qualification requires the
three exact application URLs, and each completed search checkpoint carries its
exact identity inventory. The canonical qualification directory was recreated
from an empty target after these changes.

A second independent review found that this recreated store still registered the
package-default blocked manifest even though the pilot registry was promoted.
That otherwise valid dataset remains recoverable as
`.yimby/qualification-opdc-2026-09-16.invalid-pre-readiness-fix/`. The qualifier
now registers the promoted pilot status and makes `LIVE_READY` plus HTTP
transport a named receipt predicate. The canonical directory was again
recreated from empty so its initial-cost proof and persisted manifest agree.
