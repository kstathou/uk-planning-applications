# West Suffolk qualification remediation

## Decision

Keep the change authority-local. Each committed weekly or advanced result page
adds a typed discovery proof to `WestSuffolkCheckpointV1`. The proof binds the
content digest and response URL to the query key, page number, safe request URL,
HTTP method, and non-secret form values. The existing discovery-evidence table
independently records the same association.

Qualification reads both copies, requires exact agreement, rehydrates the
content-addressed response, and runs the production result parser. Query pages
must be contiguous, totals must reconcile, and their reference union must equal
the current checkpoint membership. This keeps evidence ownership with the
authority adapter and avoids a shared storage migration.

## Receipt contract

Schema version 2 adds three SHA-256 commitments:

- cumulative current applications and their ordered detail captures;
- the current scope's ordered discovery proofs; and
- the complete retained content-digest set.

It also records the association and evidence-row counts. Missing files, invalid
gzip bodies, digest mismatches, unlinked evidence, incomplete observation
coverage, rebuild incoherence, or altered request bindings fail qualification.

Acquisition cost comes from durable authority-scoped run rows. Immediate
terminal reruns must have zero requests and zero transferred bytes. Repeating
the same scope with the same store preserves the prior receipt byte-for-byte.
A shifted weekly scope can replace the current checkpoint, while applications,
the discovery queue, evidence, and acquisition costs remain cumulative.

## Data mapping and policy

Normaliser version `west-suffolk-v3` maps the native document type and published
date into `DocumentRecord`. Document bodies remain prohibited. Representation
text remains unavailable because West Suffolk publishes it only in attachment
bodies on the recorded path.

## Alternatives considered

A generic qualification-run ledger could give every authority a shared
temporal proof model. It was rejected for this remediation because it requires a
cross-authority schema migration and creates avoidable merge risk. Its useful
property, durable acquisition provenance, is retained through existing run-cost
rows and content commitments.

The implementation was completed sequentially because no isolated worker slot
was available. The behavior tests and repository gates remain the independent
verification boundary. A fresh real-portal version 2 receipt is still required;
the committed version 1 receipt is historical evidence only.
