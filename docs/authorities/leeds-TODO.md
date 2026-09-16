# Leeds qualification TODO

The authority implementation is checkpointed, but live qualification is
incomplete because the official source repeatedly became unavailable.

Preserved state:

- 1,148 applications were discovered and persisted.
- The checkpoint is at query 10 of 43, page 10, row 90.
- No qualification receipt was issued.
- The retained SQLite store passed its integrity check.

When the official portal is stable, resume the same run:

```bash
PYTHONPATH=src python scripts/qualify_leeds.py \
  --confirm-live \
  --resume \
  --data-dir .yimby/qualification-leeds-2026-09-16 \
  --start 2026-08-18 \
  --end 2026-09-16 \
  --include-open
```

Then complete the bootstrap, verify the receipt against retained evidence, run
two real weekly refresh cycles, repeat the full repository gates, and obtain a
no-comments review of the exact final commit.
