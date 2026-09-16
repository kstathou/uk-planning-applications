# Arun follow-up

This branch contains the completed Arun adapter and qualification hardening, but
the fresh exact-head live qualification was paused at the user's request on 16
September 2026. It is not yet eligible for a final live-ready claim.

## Preserved state

- Data directory: `.yimby/qualification-arun-main-2026-09-16`
- Scope: 18 August to 16 September 2026, including older open applications
- Discovery checkpoint: ready for query 24 of the 60-query plan
- Durable rows: 470 discovered, 469 persisted, one pending retry
- Last run status: interrupted by an intentional keyboard interrupt
- SQLite integrity: `ok`
- No qualification receipt was issued by this interrupted run

## Resume

From this branch, run:

```console
PYTHONPATH=src python scripts/qualify_arun.py \
  --confirm-live \
  --resume \
  --data-dir .yimby/qualification-arun-main-2026-09-16 \
  --start 2026-08-18 \
  --end 2026-09-16 \
  --include-open
```

After the bootstrap and immediate refresh complete, record the exact receipt and
database evidence in `docs/authorities/arun.md`. Two genuinely later weekly
cycles are still required by `docs/plan.md`; do not substitute same-day reruns.
