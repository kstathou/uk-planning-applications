# Haringey handoff checkpoint

Stopped on 16 September 2026. The preserved implementation and evidence base is
commit `be6c1cab842bab583f0e99feb8901e007fe8c816`; this checkpoint is committed on
branch `codex/haringey-checkpoint`.

## What works

- The typed Haringey adapter and authority-owned Playwright page object implement
  the captured Arcus rolling-seven-day route, reconcile source-reported pages and
  counts, route by Salesforce record ID, and parse detail, comments, and file
  metadata.
- Attachment bodies are outside the collection contract and are blocked before a
  body request. The last recorded live smoke found 61 results across seven pages,
  retained ten visible references, transferred 119,220 bytes in one request, and
  made zero attachment-body requests.
- The authority-specific qualifier persists failures, validates retained evidence,
  requires an exact 30-day inclusive window plus older-open coverage, supports
  resumable state, checks immediate idempotence, and writes a typed version-1
  receipt atomically only when every completeness invariant passes.
- Official-source investigation is recorded in
  `docs/authorities/haringey.md` and
  `docs/evidence/haringey-qualification-blocker-2026-09-16-v1.json`. It exhausted
  the advertised Haringey iShare layers, the GLA Planning London Datahub, national
  Planning Data datasets, and the Salesforce sitemap before any further browser
  work.
- At the preserved implementation commit, Ruff, formatting, mypy, all 344 tests
  with 100% branch coverage, pilot verification, and package build passed. A fresh
  exact-head independent review returned `NO COMMENTS`.

## Incomplete and blocked

- Haringey is deliberately `BROWSER_ONLY`, not live-ready. No qualification
  receipt exists and readiness must not be promoted.
- The 826-record legacy-current map cohort contains PKIDs but no public HGY
  references or Salesforce identities. Officially resolved PKIDs: 0; unresolved:
  826; ambiguous: 0.
- The independently reconciled 13,974-record legacy-decided layer has no PKID
  overlap with that cohort. The GLA and Salesforce inventories expose useful
  HGY-to-Salesforce identities but no PKID. National Planning Data has no Haringey
  planning-application inventory; its PKID-bearing brownfield records also have
  zero overlap.
- Consequently, neither complete older-open collection nor the exact 30-day plus
  older-open inventory can be proved. Address/proposal matching is not an accepted
  identity crosswalk.
- The bounded 30-day discovery, terminal checkpoint, durable inventory agreement,
  immediate zero-network refresh, and both genuinely later weekly refreshes have
  not passed. Do not infer credit for any of them from the smoke or map evidence.

## Exact persisted and live state

- Operational directory:
  `.yimby/qualification-haringey-2026-09-16/` (ignored by Git; preserve it).
- `yimby.sqlite3` contains two Haringey runs, both `failed` with
  `HaringeyWindowUnavailableError`, zero requests, and zero transferred bytes. It
  contains zero applications, native versions, evidence rows, checkpoints,
  retries, documents, and comments.
- `yimby.sqlite3-shm` exists; `yimby.sqlite3-wal` currently exists at zero bytes.
  Do not delete either sidecar or copy only the main database while SQLite may be
  open.
- The operational `source-blocker-v1.json` and tracked evidence JSON are
  byte-identical. Their SHA-256 is
  `3e8f7ea3815a168f3ec6afcd35bbbd54902becef80ba4cf84854a1a88f5236dc`.
- `haringey-qualification-v1.json` and randomized temporary receipt files are
  absent. The last qualification attempt exited 1 before launching a browser.
- Last observed live counts are evidence snapshots, not constants. Re-read every
  advertised and observed count on a future run.

## Safest resume sequence

Start by restoring and inspecting this checkpoint without changing live state:

```sh
git switch codex/haringey-checkpoint
git status --short --branch
shasum -a 256 docs/evidence/haringey-qualification-blocker-2026-09-16-v1.json \
  .yimby/qualification-haringey-2026-09-16/source-blocker-v1.json
cmp docs/evidence/haringey-qualification-blocker-2026-09-16-v1.json \
  .yimby/qualification-haringey-2026-09-16/source-blocker-v1.json
sqlite3 -readonly .yimby/qualification-haringey-2026-09-16/yimby.sqlite3 \
  "SELECT r.started_at, d.status, d.failure_message FROM runs r JOIN run_details d ON d.run_id = r.id ORDER BY r.started_at;"
```

Before changing readiness, obtain an official, deterministic, zero-missing,
zero-ambiguity PKID-to-HGY-to-Salesforce crosswalk for all 826 legacy-current
records. Re-run official APIs, open-data/download sources, and official
Salesforce/Arcus/GIS machine endpoints before considering more browser scraping.
Never download attachment bodies.

After implementing a source-backed completeness path, run focused and full gates:

```sh
uv run pytest tests/test_browser_authorities_live.py -k haringey
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run python scripts/verify_pilot.py
uv build --no-sources
```

Only then repeat the preserved historical qualification attempt, explicitly
resuming its non-empty directory:

```sh
uv run python scripts/qualify_haringey.py \
  --confirm-live \
  --data-dir .yimby/qualification-haringey-2026-09-16 \
  --start 2026-08-18 --end 2026-09-16 --include-open --resume
```

That command is expected to remain fail-closed until the production adapter can
prove the required inventory and crosswalk. For a genuinely new live qualification
window, use a new date-specific data directory and an exact 30-day inclusive
range; do not overwrite this historical evidence. Promote readiness only after a
valid receipt, immediate zero-network refresh, clean exact-head gates, and a fresh
independent `NO COMMENTS` review.
