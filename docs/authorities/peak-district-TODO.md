# Peak District completion checkpoint

Checkpoint date: 16 September 2026.

The implementation is complete for request-bound discovery evidence, document
metadata normalisation, and changed-scope weekly qualification. The new paths
have deterministic test coverage. Do not mark Peak District operationally
qualified until both later live cycles below succeed.

## Preserved bootstrap state

Keep using `.yimby/qualification-peak-district-2026-09-16`. Its
`peak-district-qualification-proof-v1.json` file is the immutable private proof
for the accepted 18 August through 16 September bootstrap. Its
`peak-district-qualification-v1.json` file is the matching local receipt.

The bootstrap predates request-bound discovery registrations. This is a known
historical boundary, not a reason to rewrite the proof. The qualifier validates
the original proof before it starts cycle 1. Each later proof links to the
SHA-256 digest of the preceding proof.

Do not delete the SQLite database, the `evidence` directory, either bootstrap
file, or a completed weekly proof. Do not use a new data directory for the
weekly cycles. The cumulative store is part of the proof.

## Remaining live work

The following work remains incomplete:

- Run weekly cycle 1 on or after 23 September 2026. No live cycle 1 proof or
  receipt exists at this checkpoint.
- Run weekly cycle 2 on or after 30 September 2026. No live cycle 2 proof or
  receipt exists at this checkpoint.
- Confirm through those live runs that AssureLive still accepts the retained
  search-form, advanced-form, result, and pagination requests.
- Confirm through those live runs that document type and published date reach
  `DocumentRecord.category` and `DocumentRecord.published_date` under
  normaliser version `peak-district-v5`.
- Run the full repository gate and an independent no-comments review before
  merging this checkpoint. The focused Peak District tests, lint, formatting,
  and strict typing are green. The independent review could not start before
  this pause because all agent slots were occupied.

## Resume cycle 1

On or after 23 September 2026, run:

```sh
uv run python scripts/qualify_peak_district.py \
  --confirm-live \
  --resume \
  --data-dir .yimby/qualification-peak-district-2026-09-16 \
  --start 2026-08-25 \
  --end 2026-09-23 \
  --include-open
```

Success creates these append-only files:

- `peak-district-weekly-cycle-1-proof-v1.json`
- `peak-district-weekly-cycle-1-receipt-v1.json`

Run the same command again. The qualifier must return the same receipt without
opening a source session. If publication stopped after the temporary proof was
written, the command validates and publishes that proof without recollection.

## Resume cycle 2

On or after 30 September 2026, run:

```sh
uv run python scripts/qualify_peak_district.py \
  --confirm-live \
  --resume \
  --data-dir .yimby/qualification-peak-district-2026-09-16 \
  --start 2026-09-01 \
  --end 2026-09-30 \
  --include-open
```

Success creates these append-only files:

- `peak-district-weekly-cycle-2-proof-v1.json`
- `peak-district-weekly-cycle-2-receipt-v1.json`

Run the same command again and require a zero-network recovery. Then verify
that both original bootstrap files are byte-identical to their pre-cycle
copies. Only after both cycles pass can the operational qualification status
change from pending.

## Failure handling

Stop on `bootstrap-provenance`, `cycle-provenance`, or any failed qualification
check. Preserve the data directory and inspect the reported proof and store
state. Do not replace a proof, edit a receipt, relax a check, or start a fresh
store to bypass the failure.
