# Birmingham checkpoint

Work stopped cleanly on 16 September 2026. The reviewed implementation base is
commit `d99c6e6`; the checkpoint-document commit is reported in the handoff.
Birmingham remains deliberately `BLOCKED` and must not be promoted to live
ready from the evidence below.

## What works

- `scripts/qualify_birmingham.py` performs the bounded official ArcGIS
  qualification, retains every response, verifies the exact schema and query
  predicates, recomputes the receipt from retained evidence, and writes the
  typed receipt last.
- Non-fixture Birmingham adapter calls reject the synthetic Northgate routes
  before transport.
- The 18 August through 16 September 2026 query returned 70 unique applications
  in terminal pages of 25, 25, and 20. The latest received record was
  `2026/05750/PA` on 15 September 2026.
- No attachment body was requested. SQLite integrity, evidence hashes, and
  zero-request offline replay are verified.
- The final repository gate passed 377 tests with 100% branch coverage, Ruff,
  formatting, MyPy, locked dependency sync, and both package builds. The final
  independent review returned `NO COMMENTS`.

## Persisted local state

The live artifact is intentionally ignored by Git and exists only in this
worktree. Preserve it before deleting or replacing the worktree.

- Directory: `.yimby/qualification-birmingham-2026-09-16/`
- Receipt: `.yimby/qualification-birmingham-2026-09-16/birmingham-qualification-v1.json`
- Receipt SHA-256:
  `3cadc04df74d61e34580f55faa1d1b4e417efc5f14f3a0a17a5f39c63ce5ed58`
- Evidence: 14 content-addressed official ArcGIS response bodies
- Database: `.yimby/qualification-birmingham-2026-09-16/yimby.sqlite3`
- Registry readiness: `blocked`
- Weekly cycles 1 and 2: `pending`

## What remains blocked

- The legacy Northgate portal returned HTTP 503.
- ArcGIS recency is bounded, but source continuity is not proven. Observed
  counts were 5,802 in 2025, 453 in July 2026, 121 in August 2026, and 70 in
  the rolling 30-day window. The source does not explain the decline.
- The maximum accepted date was the future date 27 October 2026.
- The layer cannot safely enumerate older-open applications or active appeals.
  It has 3,276 null application decisions and 1,419 narrower unresolved
  candidates, with counterexamples from issued amendments, closed appeals,
  enforcement, and master-plan records.
- A complete detail route and documents, comments, conditions, consultations,
  and relationships are not exposed. These sections are unknown, not empty.
- No bootstrap checkpoint or weekly collection may be claimed until a complete
  source boundary is independently proved.

## Safest resume

Start with the retained evidence. These commands make no network request:

```sh
git switch codex/birmingham-checkpoint
UV_CACHE_DIR=/tmp/yimby-uv-cache UV_PYTHON_INSTALL_DIR=/tmp/yimby-uv-python uv lock --check
UV_CACHE_DIR=/tmp/yimby-uv-cache UV_PYTHON_INSTALL_DIR=/tmp/yimby-uv-python uv sync --locked
UV_CACHE_DIR=/tmp/yimby-uv-cache UV_PYTHON_INSTALL_DIR=/tmp/yimby-uv-python uv run --frozen pytest tests/test_birmingham_qualification.py
UV_CACHE_DIR=/tmp/yimby-uv-cache UV_PYTHON_INSTALL_DIR=/tmp/yimby-uv-python uv run --frozen python -c "from pathlib import Path; from scripts.qualify_birmingham import replay_persisted_state; print(replay_persisted_state(Path('.yimby/qualification-birmingham-2026-09-16')).model_dump_json())"
shasum -a 256 .yimby/qualification-birmingham-2026-09-16/birmingham-qualification-v1.json
git status --short
```

Expected replay output is
`{"request_count":0,"sqlite_integrity":"ok","evidence_integrity":"verified"}`
and the hash must match the value above.

Do not reuse the non-empty persisted directory for a live run. A deliberate
live requalification requires `--confirm-live`, a new empty data directory,
and a test-first update of the hard-coded scope and observed contract before
using a later date window. Keep Birmingham blocked unless that new evidence
proves completeness, older-open membership, active appeals, detail extraction,
and all required child sections.
