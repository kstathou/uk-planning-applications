# Camden live qualification design

## Decision

Use the full-prefix, typed checkpoint design from Architect Arena candidate 1 as the base. Put that state machine in a Camden-local discovery module and graft candidate 2's authority-linked evidence proof, application-table identity proof, backward-compatible native fields, exact legacy fixture migration, atomic receipt, scope-safe resume, and zero-I/O rerun checks.

## Supported live query inventory

The adapter owns one ordered inventory derived from the requested scope.

1. `DATE_RECEIVED` over the inclusive date range.
2. `DATE_VALID` over the inclusive date range.
3. `DATE_DECISION` over the inclusive date range.
4. status code `4`, `REGISTERED`, when older open applications are requested.
5. status code `14`, `APPEAL LODGED`, when older open applications are requested.

Semantic dates remain ISO dates in models and receipts. Only the form boundary renders `DD-MM-YYYY`.

## Durable state

The checkpoint persists the scope, exact typed inventory, completed query results, the active query's complete committed prefix, and the deterministic global union. It never persists `XMLLoc` or another session token.

On resume, the adapter opens a fresh form, resubmits the active query, and replays every committed page with the newly issued pagination links. The replay must reproduce the saved ordered `(reference, locator)` prefix and reported total before new rows can be accepted. First-page-only replay was rejected because a same-total replacement on a previously committed later page would escape detection.

Every checkpoint validates these invariants at construction and deserialization.

- Stored inventory equals the inventory derived from scope.
- Completed queries are an exact inventory prefix.
- Each completed membership is unique, has stable locators, and matches its reported total.
- Active prefix length equals the next offset and is smaller than its reported total.
- Global seen references equal the deterministic union of completed memberships and the active prefix.
- Terminal state contains every completed query and no active progress.
- Extra fields and session-owned tokens are rejected.

## Boundary contracts

The official form is one POST `form#M3Form`. Successful controls are retained in source order, including duplicate names. Disabled, unnamed, unchecked radio or checkbox, file, reset, and unactivated submit controls are omitted. The Camden-owned fields are replaced in place. The search submit is the only activated submit control.

Non-empty result pages must reconcile one `Records X to Y of Z` marker with the rendered application rows. The requested offset, 10-row page size, reported total, reference, numeric `PARAM0`, and next offset must agree. Repeated visual pager controls are allowed only when their normalized targets are identical. The exact observed empty marker is `No Records Found. Please resubmit search with different criteria.` Unknown shapes fail closed.

The observed detail shape is a `.dataview` list item containing one label `span` followed by value text in the same `div`. Historic records may omit both BNG coordinates. A half-present pair fails. CMWebDrawer requires `Summary` to report `Records: N`, one `table.recordtable`, and exactly `N` metadata rows. Attachment URLs are retained as metadata and never requested.

## Identity and qualification proof

Preserve `camden-jsf-search` as Camden's durable application source identity to avoid duplicating existing fixture and exact-reference records. The locator remains the numeric Northgate `PARAM0` and must stay stable for a public reference.

The qualification receipt proves the exact query inventory and per-query reported and enumerated memberships, terminal checkpoint coherence, locator-aware checkpoint and discovery-queue agreement, application-table identity agreement, rebuild-input agreement, successful current detail and document sections, no retries, no unmapped records, SQLite integrity, authority-linked evidence registration and gzip content digests, zero attachment requests, and an immediate terminal rerun with zero requests and bytes. Hashes make the compared sets inspectable but do not replace direct set equality.

Later weekly cycles remain typed pending records dated seven and fourteen days after bootstrap. The command does not claim those cycles succeeded.

## Module boundary

- `src/yimby/authorities/camden/discovery.py` owns the GeneralSearch protocol, query types, checkpoint state machine, form parsing, pagination, deduplication, and resume replay.
- `src/yimby/authorities/camden/adapter.py` owns exact-reference resolution, detail and document extraction, native models, and normalisation.
- `src/yimby/store.py` owns read-only application identity and evidence-integrity facts because it owns the SQLite and content-addressed storage schema.
- `scripts/qualify_camden.py` owns acceptance policy, receipt composition, process locking, the two-pass run, and atomic output.

## Explicit limits

The receipt proves complete execution of the five recorded source queries and fail-closed reconciliation of their pages. It does not claim all five queries are one immutable portal snapshot. The immediate rerun proves terminal restart behavior, not weekly refresh behavior. Historical coverage and active-status exhaustiveness are documented as observed source semantics, not generalized beyond Camden's official register.
