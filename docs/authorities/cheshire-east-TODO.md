# Cheshire East handoff checkpoint

Work stopped on 16 September 2026. The reviewed implementation baseline is
commit `f99ea4c19668bae85b178d69946fc51c6f538d99`; this checkpoint is the only
change after that baseline.

## Current state

- Status is `BLOCKED`, not `LIVE_READY`. Normal collection must continue to
  fail closed.
- The official API, open-data, ArcGIS/GIS, export, and dump census found no
  complete statutory planning-application inventory. Exact requests and the
  distinction between retained evidence and browser observations are recorded
  in `docs/authorities/cheshire-east.md`.
- The automated portal transport receives an AWS WAF JavaScript challenge
  instead of the search form. An interactive browser was observed to resolve
  it, but that is not a stable collector transport and those browser bodies
  were not retained as qualification evidence.
- The parser supports both observed search discriminators (`fa=""` and
  `fa="search"`), the current nine-column result table, the weekly route,
  numeric detail locators, and document metadata without downloading
  thumbnails or attachments.
- Positive search and weekly pages remain deliberately non-terminal unless the
  source publishes a complete count or an exact terminal boundary. The latest
  observations were 30 rolling-window rows, 30 `Not Determined` rows, and 50
  historical weekly rows, all without a completeness signal.
- The full suite passed with 421 tests and 100% statement and branch coverage.
  Ruff, formatting, mypy, pilot verification, package build, both hook stages,
  and independent exact-head review also passed (`NO COMMENTS`).

## Persisted qualification state

- Receipt:
  `.yimby/qualification-cheshire-east-2026-09-16/cheshire-east-qualification-blocker-v2.json`
- Receipt SHA-256:
  `76cf8d91d46af14bd30a9c454030905e7511a99c82132e872801c7514ec50139`
- Retained response content digest:
  `83484ad24f05ea30a960be0f304fd52eb90aba0c05612ae0ad3de5c8ffa24642`
- The receipt records one 2,019-byte official response, four pending source
  stages, zero attachment requests, no operational SQLite store, and pending
  weekly cycles due 23 and 30 September 2026.
- Immediate `--resume` replay completed in 0.36 seconds with the expected
  blocked exit and `offline_rerun_request_count=0`.
- `.yimby/` is ignored but intentional evidence. Do not delete, rewrite, or
  replace this receipt when resuming.

## What remains

Promotion requires either a complete official machine-readable source or a
stable authorised portal transport that can prove both:

1. the inclusive rolling 30-day application inventory; and
2. every older application that is still open.

The source must expose defensible freshness, total/terminal, and stable identity
facts. A capped-looking positive page is not proof. A successful replacement
qualification must still request document metadata only, create a durable
receipt/store, pass an immediate zero-network refresh, and complete the required
weekly observation cycles before promotion.

## Safest resume sequence

First confirm the preserved state and replay it offline. Exit status `1` is the
expected blocked result:

```sh
git status --short --ignored
shasum -a 256 .yimby/qualification-cheshire-east-2026-09-16/cheshire-east-qualification-blocker-v2.json
UV_CACHE_DIR=/private/tmp/uv-cache uv run python scripts/qualify_cheshire_east.py --confirm-live --data-dir .yimby/qualification-cheshire-east-2026-09-16 --start 2026-08-18 --end 2026-09-16 --include-open --resume
```

Recheck locally without contacting the council:

```sh
UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest --no-cov tests/test_cheshire_east_qualification.py
UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest
UV_CACHE_DIR=/private/tmp/uv-cache uv run ruff check .
UV_CACHE_DIR=/private/tmp/uv-cache uv run mypy src tests scripts
```

Only after redoing the API-first census should a new live qualification be
attempted. Use a new empty data directory, set `--end` to the live date, and set
`--start` to 29 days earlier so the inclusive window is exactly 30 days:

```sh
UV_CACHE_DIR=/private/tmp/uv-cache uv run python scripts/qualify_cheshire_east.py --confirm-live --data-dir .yimby/qualification-cheshire-east-YYYY-MM-DD-new --start YYYY-MM-DD --end YYYY-MM-DD --include-open
```

Do not reuse the preserved data directory for a new probe, infer terminality
from row counts, bypass access controls, or request attachment bodies. If the
WAF or completeness blocker remains, retain the new blocked receipt and stop.
