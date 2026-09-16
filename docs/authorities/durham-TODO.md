# Durham qualification handoff

This work is intentionally stopped as an incomplete, resumable checkpoint. Do
not claim Durham as `LIVE_READY`: the bootstrap receipt was not emitted and the
two later weekly cycles required by `docs/plan.md` have not run.

## Implemented and verified locally

- The Durham IDOX adapter owns the live advanced-form contract, weekly search,
  strict result counts and pagination, complete older-open query partitions,
  detail extraction, metadata-only document handling, and fail-closed parsing.
- Live HTTP uses the operating-system trust store without disabling TLS
  verification, preserves cookies and form fields, retries transport failures,
  and enforces the shared two-second-per-host limit.
- Discovery and detail collection are durable and resumable. The qualification
  command validates an exact inclusive 30-day window, all 90 query keys,
  evidence paths, database integrity, section health, attachment policy,
  deduplication, and an immediate zero-request rerun before atomically writing a
  typed receipt.
- Deterministic adapter and qualification tests, documentation, and source-audit
  evidence are committed on `codex/durham-qualification` through commit
  `6785d2e4a828` before this handoff commit.

## Exact stopped state

Persisted directory:
`.yimby/qualification-durham-2026-09-16`

The live process was deliberately interrupted at
`2026-09-16T18:27:43.416492+00:00`. Its database then reported:

- 896 applications;
- 898 discovered references;
- 2,688 evidence records;
- 12 of 90 query keys complete: all ten weekly queries, `Appeal lodged`, and
  the complete 429-row `Pending Decision` older-open partition;
- `live_complete = false`, no active query, and no qualification receipt;
- one pending cancellation retry for reference `3/1989/0428`, locator
  `ZZZZZZRAXE165`. The earlier transient TLS retry for `DM/24/01043/FPA`
  succeeded on resume.

Run history contains one fail-closed `SSLError` run and one intentionally
`interrupted` run. This is expected checkpoint history, not qualification
evidence. The current registry state must remain `DISCOVERY_ONLY`.

## Remaining work

1. Resume the same persisted directory and allow the remaining 78 older-open
   query partitions and all queued details to finish.
2. Require `durham-qualification-v1.json` to show all checks true, 90 completed
   query keys, zero pending retries and failed sections, no attachment-body
   requests, matching application/reference counts, and an immediate rerun with
   zero requests. Do not infer success from the database counts alone.
3. Update the Durham walkthrough, pilot acceptance record, registry evidence,
   and both audit TSVs with the receipt's exact counts and costs. Keep Durham
   `DISCOVERY_ONLY` while the two later weekly cycles remain pending.
4. Run the complete repository gates, clean temporary state, commit the exact
   head, and obtain a fresh independent exact-head review whose result is
   exactly `NO COMMENTS`.
5. Run and record the two later weekly cycles before any operational-readiness
   promotion.

## Safest resume

From the repository root, preserve the directory exactly and use `--resume`:

```console
UV_CACHE_DIR=.uv-cache uv run python scripts/qualify_durham.py \
  --confirm-live \
  --resume \
  --data-dir .yimby/qualification-durham-2026-09-16 \
  --start 2026-08-18 \
  --end 2026-09-16 \
  --include-open
```

Do not remove the SQLite database, evidence directory, checkpoint, or lock-file
path. The advisory lock itself is released because the process has stopped. If
the command fails, inspect the recorded run, retry queue, and checkpoint, fix
only an evidence-backed defect, and resume the same directory again.

The qualification command itself performs the required immediate second
collection and records its zero-request result in the receipt. Do not rerun and
overwrite that receipt merely to prove idempotence. After it succeeds, run the
repository's Ruff, formatting, mypy, full pytest with 100% branch coverage,
pilot verifier, build, pre-commit, pre-push, and `git diff --check` gates before
the independent review.
