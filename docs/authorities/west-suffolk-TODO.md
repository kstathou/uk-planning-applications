# West Suffolk remaining work

This file is the handoff for work intentionally left incomplete on 16 September
2026. The adapter and schema version 2 qualifier are implemented and covered by
deterministic tests. The stronger contract has not yet been run against the
real portal.

## Required before fresh live acceptance

- Survey West Suffolk's official publications for an API, open-data feed, or
  complete data dump. Record the sources checked and their coverage. Use a
  machine-readable source instead of IDOX scraping if it proves the required
  recent, older-open, and refresh inventories and fields.
- Run the schema version 2 qualifier from a new empty state directory. The
  historical state at
  `.yimby/qualification-west-suffolk-2026-09-16` contains a version 1
  checkpoint without request-bound discovery proofs and cannot establish a
  version 2 receipt. Preserve it as historical evidence; do not treat it as the
  resume state for this run.
- Compare the fresh live query inventory, counts, sample application fields,
  document types and dates, and unavailable-comment policy with the dated
  walkthrough. Confirm that no attachment body was requested.
- Review the generated version 2 receipt and evidence audit. Commit only a
  privacy-safe aggregate receipt, then update the acceptance ledger and
  registry evidence text to cite it.
- Complete genuinely later weekly refreshes around 23 September and 30
  September 2026. Same-day zero-network reruns prove idempotence but do not
  count as those cycles.
- Run the complete repository CI and an independent no-comments review before
  merging. At this checkpoint, all 517 tests passed, but the last full run
  reported 99.85% coverage before the missing defensive-branch tests were
  added. Those added tests have only fast local validation until CI reruns.

## Fresh bootstrap state and command

Use this new state directory:

```text
.yimby/qualification-west-suffolk-v2-2026-09-16
```

Start the fresh live bootstrap from the repository root:

```sh
uv run python scripts/qualify_west_suffolk.py \
  --confirm-live \
  --data-dir .yimby/qualification-west-suffolk-v2-2026-09-16 \
  --start 2026-08-18 \
  --end 2026-09-16 \
  --include-open
```

If that exact run is interrupted, or to verify its terminal zero-network
resume, use the same state and scope:

```sh
uv run python scripts/qualify_west_suffolk.py \
  --confirm-live \
  --resume \
  --data-dir .yimby/qualification-west-suffolk-v2-2026-09-16 \
  --start 2026-08-18 \
  --end 2026-09-16 \
  --include-open
```

The resume is acceptable only if it makes zero requests, transfers zero bytes,
and leaves `west-suffolk-qualification-v2.json` byte-identical.

## Later weekly scopes

After the fresh bootstrap succeeds, reuse the version 2 state for the first
later weekly scope:

```sh
uv run python scripts/qualify_west_suffolk.py \
  --confirm-live \
  --resume \
  --data-dir .yimby/qualification-west-suffolk-v2-2026-09-16 \
  --start 2026-09-21 \
  --end 2026-09-27 \
  --include-open
```

Use the same command with `--start 2026-09-28 --end 2026-10-04` for the second
later scope. Each first pass may make source requests and must add new records
without losing cumulative applications. Repeat each exact scope immediately;
the second pass must be zero-network and preserve that scope's receipt bytes.
