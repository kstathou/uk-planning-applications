# System architecture

This document explains the implementation shape for the 15-authority pilot. It is an explanation, not an operating guide.

## Operator flow

An operator selects one authority or the full pilot. The collector opens an authority package and runs its three operations.

1. `discover` returns portal references in resumable batches.
2. `fetch` returns one authority-native snapshot with an explicit state for every section.
3. `normalise` converts the snapshot into common records while retaining native values and provenance.

The collector commits each discovery batch before it advances the checkpoint. It stores evidence before it commits a database row that refers to the evidence. Repeating a completed run produces the same queue and semantic versions.

## Domain model

`AuthorityManifest` is the registry record. An authority can own several `SourceDefinition` records. Each source records its covered dates, allowed hosts, path restrictions, rate limit, and portal capabilities. This supports shared portals, legacy registers, supporting document services, and portal migrations without forcing one authority into one URL.

`SectionState` separates these source outcomes:

- `complete` means that the scraper enumerated the section.
- `partial` means that the scraper retained known items but could not prove completeness.
- `empty` means that a complete enumeration returned no items.
- `unavailable` means that the source does not expose the section.
- `excluded` means that collection policy forbids the data.
- `failed` means that collection should retry the section.

A failed section never replaces a prior complete section with an empty result.

Each authority owns a versioned Pydantic model for its native payload and checkpoint. The runtime wrapper converts those typed values to stored JSON. The collector does not depend on council-specific fields.

`NormalisedObservation` holds the common application, document, comment, event, relationship, consultation, condition, and location records. Each mapped value retains its native value, source path, source identifier, and evidence digest.

## Boundaries

Authority packages own portal navigation, parsing, native models, normalisation, fixtures, and limitations. Shared code owns HTTP and browser sessions, retries, throttling, persistence, orchestration, exports, and reporting.

Raw HTML, JSON, browser objects, and SQLite rows stay behind their adapters. Internal code receives typed domain values.

The transport accepts search, detail, document-index, comment, event, and supporting-data requests. It has no attachment-body request type. The live client rejects a response with an attachment media type before it consumes the body. Verification also compares every request against the emitted document links.

## Storage invariants

SQLite runs with foreign keys and WAL mode. One writer owns state changes. Authority workers can fetch independently, but they submit immutable commands to that writer.

The store enforces these invariants:

- Queue insertion and checkpoint advancement share one transaction.
- Application identity uses authority and portal identifiers, never an address alone.
- A semantic hash excludes transport timestamps, tokens, and irrelevant ordering.
- An unchanged observation updates freshness without adding a version.
- A changed section adds a version and retains earlier states.
- A normaliser rebuild reads retained native payloads and never contacts a portal.
- Complete enumeration is required before disappearance reconciliation.

Evidence uses gzip-compressed, content-addressed files. The store writes the file before it commits its digest. A crash can leave an unreferenced file, but it cannot leave a database row that points to a missing file.

## Module ownership

```text
src/yimby/
  api.py
  cli.py
  domain/
  adapters/
  authorities/<authority>/
  transport/
  collection/
  storage/
  verification/
  export.py
  backup.py
  dashboard.py
```

Every authority directory exports one package. It contains the manifest, native model, adapter, normaliser, fixtures, and browser page objects when the portal requires JavaScript.

The design deliberately avoids a vendor framework. Portals sold under the same product name differ in pagination, hidden fields, supporting services, and migration history. Shared transports remove operational duplication. Authority packages keep source assumptions local.

## Verification

`scripts/verify_pilot.py` is the reproducible acceptance check. It verifies the registry count, package contract, pagination, section states, idempotent reruns, interrupted-run recovery, attachment policy, offline normalisation, export policy, backup restoration, and dashboard data.

Fixture verification proves deterministic behavior. Live verification proves the current portal path. Each live result is `VERIFIED`, `NOT VERIFIED`, or `INCONCLUSIVE`. The pilot still needs two real weekly cycles before it can pass the plan's operational acceptance criterion.

