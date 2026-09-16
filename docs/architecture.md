# System architecture

This document explains the implementation shape for the 15-authority pilot. It is an explanation, not an operating guide.

## Operator flow

An operator selects one authority or the full pilot. The collector opens an authority package and runs its three operations.

1. `discover` returns portal references in resumable batches.
2. `fetch` returns one authority-native snapshot with an explicit state for every section.
3. `normalise` converts the snapshot into common records while retaining native values and provenance.

Before discovery, the collector resumes queued details, due retries, and due
refreshes. It then commits each discovery batch before it advances the
checkpoint and de-duplicates references already processed in that run. It
stores evidence before it commits a database row that refers to the evidence.
Repeating a completed run produces the same queue and semantic versions.

## Domain model

`AuthorityManifest` is the registry record. An authority can own several
`SourceDefinition` records. Each source has a stable identifier, base URL, and
optional covered dates. The manifest also carries authority-wide capabilities
and a separate `LiveStatus` with evidence, reason, and required transport. This
supports shared portals, legacy registers, supporting document services, and
portal migrations without forcing one authority into one URL.

`SectionState` separates these source outcomes:

- `complete` means that the scraper enumerated the section.
- `empty` means that a complete enumeration returned no items.
- `unavailable` means that the source does not expose the section.
- `excluded` means that collection policy forbids the data.
- `failed` means that collection should retry the section.

A failed section never replaces a prior complete section with an empty result.

Each authority owns a versioned Pydantic model for its native payload and checkpoint. The runtime wrapper converts those typed values to stored JSON. The collector does not depend on council-specific fields.

`NormalisedObservation` holds the common application, document, comment, event,
relationship, consultation, condition, and location records. The complete
authority-native JSON is retained separately. Mapped core fields carry the
evidence digest that supports them.

## Boundaries

Authority packages own portal navigation, parsing, native models, normalisation, fixtures, and limitations. Shared code owns HTTP and browser sessions, retries, throttling, persistence, orchestration, exports, and reporting.

Raw HTML, JSON, browser objects, and SQLite rows stay behind their adapters. Internal code receives typed domain values.

The transport accepts search, detail, and comment requests. It has no
attachment-body request type. The live client blocks known attachment paths and
download endpoints, rejects attachment media types or content dispositions
before it consumes the response body, and aborts image and media browser
subresources. The collector also compares retrieved request URLs with emitted
document links.

## Storage invariants

SQLite runs with foreign keys and WAL mode. One connection owns state changes.
Up to four authority coroutines can fetch independently, while synchronous
store calls serialize on the same event-loop thread and writer connection.

The store enforces these invariants:

- Queue insertion and checkpoint advancement share one transaction.
- Application identity uses authority and portal identifiers, never an address alone.
- A semantic hash excludes transport timestamps, tokens, and irrelevant ordering.
- An unchanged observation updates freshness without adding a version.
- A changed section adds a version and retains earlier states.
- Application versions include mapped common metadata, so a meaningful
  metadata-only change is not lost.
- Active and recently decided records are next due weekly; decisions older
  than 90 days are next due after 90 days.
- A normaliser rebuild reads retained native payloads and never contacts a portal.

Evidence uses gzip-compressed, content-addressed files. The store writes the file before it commits its digest. A crash can leave an unreferenced file, but it cannot leave a database row that points to a missing file.

## Module ownership

```text
src/yimby/
  adapters.py
  cli.py
  collection.py
  domain.py
  store.py
  transport.py
  http_transport.py
  browser_transport.py
  orchestration.py
  authorities/<authority>/
  migrations/
  exporting.py
  backup.py
  dashboard.py
  dashboard_app.py
  doctor.py
  normalise.py
```

Every authority directory exports one package. It contains the manifest, native
model, adapter, normaliser, and fixtures. JavaScript-dependent live page
objects remain authority-owned when implemented.

The design deliberately avoids a vendor framework. Portals sold under the same product name differ in pagination, hidden fields, supporting services, and migration history. Shared transports remove operational duplication. Authority packages keep source assumptions local.

## Verification

`scripts/verify_pilot.py` is the small reproducible fixture smoke check. It
collects the exact 15-package registry and verifies that no attachment body was
requested. The Pytest suite verifies parsing, section states, idempotent reruns,
interrupted-run recovery, live transport controls, offline normalisation,
export policy, backup restoration, and dashboard data.

Fixture verification proves deterministic behavior. Dated browser evidence and
live adapter runs are separate evidence levels. The pilot still needs real live
bootstrap and two later weekly cycles before it can pass the plan's operational
acceptance criterion.
