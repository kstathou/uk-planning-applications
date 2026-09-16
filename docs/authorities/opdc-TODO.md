# OPDC remediation checkpoint and TODO

This branch is intentionally paused. It was created from exact `origin/main`
`9eb218d506a7f37d2d04d23e16d92ffc61174f8c` in the worktree
`/private/tmp/yimby-opdc-main-remediation` on branch
`codex/opdc-main-remediation`.

## Completed on this branch

- `373ff53` preserves the official OPDC search response as request-bound
  `discovery_evidence`, preserves document category and received date during
  normalisation, and advances the normaliser version to `opdc-v3`.
- `c43cf1a` makes qualification validate the persisted discovery registrations,
  supports a later exact 30-day refresh scope without discarding cumulative
  records, and preserves the last successful public and private receipts after
  a failed refresh.
- The official Agile JSON API remains the selected source. The source audit in
  `docs/authorities/opdc.md` found no more complete official dump; rendered-page
  scraping is therefore neither preferred nor necessary.

Deterministic verification completed before this checkpoint:

- `uv run --frozen pytest -q` — 516 tests passed with 100% statement and branch
  coverage.
- `uv run --frozen pytest --no-cov -q tests/test_opdc_live.py` — 35 tests passed.
- Changed-file Ruff checks and strict mypy passed for the adapter, qualifier,
  and OPDC tests.
- The corruption suite rejects changes to `request_url`, `request_method`,
  `request_form_json`, `response_url`, `query_key`, `page`, and row deletion.

## Remaining incomplete work

1. Run a fresh live qualification with this branch. The currently committed
   `docs/evidence/opdc-qualification-2026-09-16.json` and the earlier private
   `.yimby/qualification-opdc-2026-09-16/` state predate request-bound
   `discovery_evidence`; they do not prove the strengthened boundary. Do not
   present them as proof of this remediation.
2. Inspect and sanitize the newly generated public receipt before committing
   it. Commit a receipt only after the live run succeeds; do not copy or edit a
   receipt to manufacture success.
3. Run the first and second later weekly refresh cycles when their end dates
   arrive (2026-09-23 and 2026-09-30). Confirm each refresh adds a new set of
   discovery registrations while retaining earlier registrations and
   applications.
4. Obtain an independent exact-head, no-comments review. The attempted review
   could not start because all collaboration slots were occupied before the
   pause request.
5. Before proposing a PR, run the remaining repository-wide gates at the final
   head: Ruff format/check, full mypy, pilot verification, build, and pre-commit
   plus pre-push hooks. The full pytest gate has already passed, but should be
   rerun if code changes after this checkpoint.
6. Push the branch and open a PR only when explicitly resumed. This checkpoint
   is intentionally local and has no PR.

## Exact resume state and commands

Resume in the existing isolated worktree:

```sh
cd /private/tmp/yimby-opdc-main-remediation
git status --short
git branch --show-current
git rev-parse HEAD
```

Create fresh live evidence without overwriting the older local proof directory:

```sh
uv run --frozen python scripts/qualify_opdc.py \
  --confirm-live \
  --data-dir .yimby/qualification-opdc-request-evidence-2026-09-16 \
  --end 2026-09-16 \
  --include-open
```

A successful bootstrap must persist three query registrations for its
evidence-bearing run, complete a zero-network terminal rerun, and emit a receipt
whose named `discovery-evidence` check passes. Inspect the registrations with:

```sh
sqlite3 .yimby/qualification-opdc-request-evidence-2026-09-16/yimby.sqlite3 \
  "SELECT run_id, query_key, page, response_url, request_url, request_method, request_form_json FROM discovery_evidence WHERE authority_id = 'opdc' ORDER BY run_id, query_key, page, digest;"
```

Run the two later refresh qualifications against that same durable store:

```sh
uv run --frozen python scripts/qualify_opdc.py \
  --confirm-live \
  --data-dir .yimby/qualification-opdc-request-evidence-2026-09-16 \
  --end 2026-09-23 \
  --include-open \
  --resume

uv run --frozen python scripts/qualify_opdc.py \
  --confirm-live \
  --data-dir .yimby/qualification-opdc-request-evidence-2026-09-16 \
  --end 2026-09-30 \
  --include-open \
  --resume
```

Then run the final local gates:

```sh
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy
uv run --frozen pytest -q
uv run --frozen python scripts/verify_pilot.py
uv build
uv run --frozen pre-commit run --all-files
uv run --frozen pre-commit run --hook-stage pre-push --all-files
```
