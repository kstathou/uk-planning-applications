# Barnet closeout checkpoint

Checkpoint date: 16 September 2026.

Barnet is implemented but **not live-qualified**. Preserve the code, evidence,
and retained state; do not mark the authority ready until the interrupted
bootstrap and both later live cycles complete.

## What works

- The live adapter covers weekly validated and decided lists, the exact
  received-date range, four native older-open states, and five active-appeal
  states. Pagination, displayed totals, stable reference/locator identities,
  and resumable checkpoints fail closed.
- Summary, document metadata, public comments, and consultee comments are
  collected without downloading attachment bodies. A zero child count is
  accepted only from a successfully loaded section.
- The qualification command uses a ten-second minimum request gap, makes no
  automatic retry after HTTP 429, commits progress page by page, and requires
  SQLite integrity, evidence rehashing, exact durable identity agreement,
  complete sections, and an immediate zero-I/O rerun before writing a receipt.
- Qualification lineage preserves the original successful bootstrap date once
  one exists. Receipt and lineage corruption fail before live source I/O.
- The official Open Barnet/DataPress catalogue has been assessed offline. Its
  planning dataset is a historical decided-application fallback ending March
  2021; the only newer planning-application match is explicitly internal dummy
  PDF proof-of-concept data. It cannot replace Public Access.
- Deterministic status at closeout: 370 tests pass with 100% statement and
  branch coverage; Ruff, formatting, mypy, pre-commit, and pre-push pass. The
  exact implementation and evidence review returned `NO COMMENTS`.

## Exact retained and live state

- Retained target: `.yimby/qualification-barnet-2026-09-16`.
- Fixed inclusive scope: `2026-08-18` through `2026-09-16`, with older-open
  discovery enabled. Do not change these dates when resuming this target.
- Sanitized counts: 26 requests, 10 discovered references, 6 persisted
  applications, 24 evidence records, and 1 pending retry.
- Checkpoint: page 2 of the first weekly validated partition; discovery is not
  terminal. The older-open and active-appeal partitions were not reached.
- SQLite integrity: `ok`. Qualification receipt: absent. Qualified lineage:
  absent. Both approximately 7-day and 14-day follow-up cycles remain
  `pending-bootstrap`.
- Live blocker: the official Public Access source returned HTTP 429 during
  detail collection. The command stopped and retained the active work. Do not
  interpret the partial rows as a completed bootstrap.
- Still unproved live: non-empty comment and document pagination, a complete
  exact-scope bootstrap, and the two later weekly refresh cycles.
- Committed evidence:
  `docs/evidence/barnet-qualification-blocker-2026-09-16.json` and
  `docs/evidence/barnet-open-data-assessment-2026-09-16.json`.

## Safest resume

First open one ordinary official Public Access page manually. If it still
returns 429, stop; do not run the command. Do not use parallel collectors,
rotate clients or identities, add retries, create a fresh target, or substitute
the historical Open Data files.

Once the ordinary official page is healthy, resume the same target exactly:

```sh
uv run python scripts/qualify_barnet.py \
  --confirm-live \
  --data-dir .yimby/qualification-barnet-2026-09-16 \
  --start 2026-08-18 \
  --end 2026-09-16 \
  --include-open \
  --resume
```

If the command encounters another 429, leave the target untouched and wait
again. After a successful terminal bootstrap, require the generated receipt
and run the two scheduled refresh cycles at their real due dates; same-day
reruns do not satisfy them.

To reproduce the current blocker artifact offline, and only after independently
confirming that the official page still returns 429:

```sh
uv run python scripts/export_barnet_blocker.py \
  --data-dir .yimby/qualification-barnet-2026-09-16 \
  --confirm-official-http-429
```

Useful offline verification before any live resume:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```
