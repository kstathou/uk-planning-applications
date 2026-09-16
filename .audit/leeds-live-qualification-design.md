# Leeds live qualification design

## Caller contract

The existing collector interface stays unchanged.

```python
report = await Collector(registry, store).collect(
    AuthorityId("leeds"),
    DiscoveryWindow(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=True,
    ),
    session,
)
```

The qualification tool owns consent, the exact 30-day scope, the isolated data directory, the same-day proof, and the immediate zero-network rerun.

```text
uv run python scripts/qualify_leeds.py \
  --confirm-live \
  --data-dir .yimby/qualification-leeds-2026-09-16 \
  --start 2026-08-18 \
  --end 2026-09-16 \
  --include-open
```

An interrupted command uses the same arguments plus `--resume`. A successful command writes `leeds-qualification-v1.json` atomically. It proves the dated bootstrap only. Two later weekly cycles remain typed pending states, and Leeds remains below `LIVE_READY`.

## Grounded discovery model

One private typed inventory owns execution. The qualifier independently reconstructs the expected keys and compares them with the terminal checkpoint.

```python
class _WeeklyQuery(FrozenModel):
    week: str
    date_type: Literal["DC_Validated", "DC_Decided"]


class _DateRangeQuery(FrozenModel):
    kind: Literal["validated", "decision"]
    start: date
    end: date


class _CurrentCaseTypeQuery(FrozenModel):
    case_type: str


class _ActiveAppealQuery(FrozenModel):
    appeal_status: Literal["Appeal lodged"] = "Appeal lodged"
```

For 18 August through 16 September 2026, the exact ordered inventory has 43 queries.

1. Ten weekly queries cover five intersecting Mondays and both weekly date types.
2. Two advanced date queries cover validated and decision dates for the exact inclusive window.
3. Thirty `Current` queries cover every investigated nonblank case type, including `Unknown`.
4. One `Appeal lodged` query covers active appeals whose application status is no longer current.

The official unpartitioned `Current` query returned `Too many results found. Please enter some more parameters.` A cap is a distinct failure. It is never empty or complete. A capped case-type partition blocks qualification because no second exhaustive partition is proven.

The advanced parser requires the exact action, `POST` method, enabled control inventory, status values, appeal values, and the 30 investigated case-type value and label pairs. It preserves every enabled named control in DOM order. It replaces only fields owned by the active query and preserves `caseAddressType=Application`, `_csrf`, `searchType`, blank defaults, and repeated opaque fields. Taxonomy drift fails closed.

## Checkpoint

The checkpoint stays close to the proven West Suffolk structure. It records the exact scope, ordered completed query keys and reported totals, active query key, next page, active row count, stable source identities, and a terminal flag. It does not persist form secrets, form contracts, page bodies, or page-level proof objects.

```python
class LeedsQueryCompletionV1(FrozenModel):
    key: str
    reported_total: int


class LeedsReferenceIdentityV1(FrozenModel):
    reference: str
    locator: str


class LeedsCheckpointV2(FrozenModel):
    result_page: str
    live_scope: LeedsDiscoveryScope | None
    completed_queries: tuple[LeedsQueryCompletionV1, ...]
    active_query: str | None
    next_page: int
    query_row_count: int
    seen_references: tuple[LeedsReferenceIdentityV1, ...]
    live_complete: bool
```

The same reference cannot acquire another locator. The same locator cannot acquire another reference. A terminal checkpoint returns a complete empty batch before form access. A resumed page above one replays the query's first-page POST to restore the portal session, then requests the saved page. One `_advance_checkpoint` helper owns state transitions after row and reported-total checks pass.

## Detail boundary

The adapter fetches only summary and document-index pages. It preserves the verified remote-exception error and requires the published reference to equal the queued reference.

`Reference`, `Proposal`, and `Status` are required and nonblank. `Application Validated`, `Address`, `Application Type`, `Appeal Status`, and `Appeal Decision` are optional native values because the successful sample did not prove that every older record populates them.

The live document table has six cells per row. The first is a proven empty structural cell. The remaining cells are `Date Published`, `Document Type`, `Measure`, `Description`, and `View`. The parser stores metadata and resolved view URLs but never requests them. A header-only documents table is an empty section. A missing or malformed table without an explicit empty marker is a failed section. Public comment text is `UnavailableSection` under the verified Leeds policy, and comment tabs are not fetched.

Normalisation maps proposal, status, optional address and validated date, and document metadata. It retains appeal fields only in the native payload and advances to `leeds-v2`.

## Qualification receipt

`LeedsQualificationReceiptV1` records the authority, exact scope, ordered query inventory with observed totals, durable counts, initial and rerun costs, exact set agreement, SQLite integrity, retained evidence integrity, two successful run statuses, two typed pending weekly cycles, and named checks. `live_ready_promoted` is always false in this bootstrap receipt.

The set proof compares sorted canonical `(source_id, reference, locator)` triples before hashing them. It compares the terminal checkpoint, the durable discovery queue, and Leeds application views or retained native records through public store APIs. Equal counts alone do not pass.

The first run must leave a nonzero application set, no retry, no current failed section, no unmapped record, and no attachment-body request. The second run uses a new session and must make zero requests, transfer zero bytes, attempt zero attachments, and leave the qualification snapshot and set proof unchanged. Search response bodies are not claimed as retained evidence. Existing retained application captures are rehydrated and rehashed.

## Module map

- `src/yimby/authorities/leeds/adapter.py` owns the form contract, typed query inventory, pagination, checkpoint, cap handling, summary parser, document parser, native models, and normaliser.
- `src/yimby/authorities/leeds/__init__.py` binds the evolved Leeds application and checkpoint models.
- `scripts/qualify_leeds.py` owns CLI safety, process locking, receipt types, public store checks, the immediate rerun, and atomic receipt writing.
- `tests/test_idox_live.py` owns deterministic live-shaped behavior tests unless the file becomes an observed maintenance problem.
- `docs/authorities/leeds.md`, `docs/portal-inventory.md`, `docs/pilot-acceptance.md`, `docs/operations.md`, and `src/yimby/registry.py` report only what the persisted receipt proves.
- `.yimby/qualification-leeds-2026-09-16` owns the local database, indexed evidence, and receipt.

No generic IDOX layer, shared store migration, or transport change is introduced.

## Arena synthesis

Candidate A won narrowly because it fits the current collector and public store APIs. The synthesis keeps its authority-local adapter, terminal fast path, capped-partition boundary, application evidence checks, and pending-cycle receipt.

Candidate B contributed the single typed query inventory and full source-identity set comparison. Those ideas prevent adapter and receipt drift and prevent count-only false agreement.

The synthesis rejects both candidates' omitted weekly reconciliation, five-cell document assumption, and over-strict optional summary fields. It rejects dynamic taxonomy acceptance because the complete older-open proof depends on the exact investigated 30-type union. It also rejects page-level proof objects, persisted form contracts, private SQLite access, and an unindexed discovery-evidence wrapper. Those structures add state without a public persistence path or a stronger qualification claim.

## Verification sequence

1. Commit red tests for the form boundary, exact inventory, and rendered fields.
2. Commit green form and query planning code.
3. Commit red tests for pagination, cap, locator conflict, resume, and terminal zero I/O.
4. Commit green discovery and checkpoint code.
5. Commit red tests for summary identity, optional values, six-cell documents, unavailable comments, and attachment avoidance.
6. Commit green detail and normalisation code.
7. Commit red qualification tests, then green tooling and receipt code.
8. Run the official live qualification and update documentation only from its receipt.
9. Run the full gates, a cleanup pass, and an independent review whose complete result is exactly `NO COMMENTS`.

## Accepted risks

- A case-type partition can cross the portal cap. The correct result is a blocker naming that query.
- The source offers no snapshot isolation. Query totals and fail-closed pagination reduce but cannot remove live drift.
- Optional summary fields can be blank on older records. The native model preserves absence instead of fabricating values.
- Search bodies are not retained by current discovery persistence. The receipt proves search completion from checkpoint state, totals, queue agreement, and deterministic parsing.
