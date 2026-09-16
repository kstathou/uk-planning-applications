# Operating the 15-authority pilot

This guide covers the local pilot implementation. It separates deterministic
fixture operation from live portal qualification. A fixture run proves the
package and storage workflow; it does not prove that a current public portal is
collectable.

## Install

```sh
uv sync --locked
uv run yimby authorities
```

The default data directory is `.yimby`. Use `--data-dir <path>` before the
subcommand to select another local store. The directory contains
`yimby.sqlite3`, compressed content-addressed evidence, exports, and collection
lock state.

## Inspect authority status

```sh
uv run yimby authorities
uv run yimby --data-dir .yimby dashboard --json
uv run yimby --data-dir .yimby doctor
```

Every authority remains in the 15-authority denominator. Package readiness and
live readiness are separate fields. A blocked or partially researched portal
is a visible coverage gap, not a zero-result source.

## Deterministic collection

Use fixture mode for local development and CI:

```sh
uv run yimby --data-dir .yimby bootstrap --authority all --days 30 --include-open --fixture
uv run yimby --data-dir .yimby sync --authority all --fixture
```

Runs are idempotent at the semantic-version boundary. Replaying an unchanged
fixture updates observations and freshness without adding a duplicate semantic
version. Discovery batches and their checkpoints commit together. A failed
detail request remains in the retry queue. Each run first resumes queued and
due detail work, then performs discovery and de-duplicates any references that
were already processed. Active and recently decided records are due weekly;
records decided more than 90 days ago are due every 90 days.

## Live collection boundary

Omit `--fixture` to request live collection:

```sh
uv run yimby --data-dir .yimby bootstrap --authority barnet --days 30 --include-open
uv run yimby --data-dir .yimby sync --authority all
```

The command returns one structured result per authority and isolates failures.
Only an authority explicitly marked `live-ready` may open a live transport.
Discovery-only, browser-only, and blocked authorities return an unavailable
result with their recorded reason. The implementation must not promote an
authority to `live-ready` until its real adapter completes collection and agrees
with the dated browser walkthrough.

One kernel-owned advisory lock prevents overlapping collection commands. The
kernel lock is authoritative. Its persistent regular file contains a
diagnostic PID while collection is healthy, but stale PID text can remain
after an abnormal exit. The operating system still releases ownership when a
process exits or crashes. Symlink and non-regular lock paths are rejected. One
orchestration
allows at most four authority tasks, one browser worker, one in-flight request
per host, and at least two seconds between completed requests to the same host.
Live HTTP sessions retain cookies, use bounded retries, and apply
`Retry-After` across every session sharing that host limiter.

The Barnet adapter also has an opt-in, bounded smoke that persists a non-secret
checkpoint after every result page:

```sh
uv run python scripts/smoke_barnet.py \
  --confirm-live --week 2026-09-14 \
  --state .yimby/smoke-barnet-2026-09-14.json
```

Reuse the same state path to resume after a rate limit or interruption. The
smoke is evidence for only that authority, week, and successfully completed
sections. It does not promote the authority or satisfy a weekly cycle by
itself.

Cornwall, Durham, Leeds, and West Suffolk use the same safe opt-in boundary:

```sh
uv run python scripts/smoke_cornwall.py --confirm-live --week 2026-09-14
uv run python scripts/smoke_durham.py --confirm-live --week 2026-09-14
uv run python scripts/smoke_leeds.py --confirm-live --week 2026-09-14
uv run python scripts/smoke_west_suffolk.py --confirm-live --week 2026-09-14
```

Without `--confirm-live`, every smoke exits before constructing a live session.
Each accepts a non-secret state path for resumable pagination. Leeds stops at
its explicit unverified-detail boundary even when discovery succeeds.

The captured non-IDOX contracts have matching opt-in smokes:

```sh
uv run python scripts/smoke_arun.py --confirm-live
uv run python scripts/smoke_camden.py --confirm-live --reference 2026/2706/L
uv run python scripts/smoke_devon.py --confirm-live
uv run python scripts/smoke_peak_district.py --confirm-live
```

Arun, Devon, and Peak District restrict discovery to their recorded rolling or
received-date paths. Camden resolves one explicit reference and deliberately
rejects an unsupported bounded date search. These smokes also refuse before
creating a live session unless `--confirm-live` is present.

Cheshire East and Haringey expose their equally bounded contracts through two
additional opt-in smokes:

```sh
uv run python scripts/smoke_cheshire_east.py --confirm-live
uv run python scripts/smoke_haringey.py --confirm-live
```

The Cheshire East smoke submits only the current date as the recorded
valid-date-from input. It returns the visible references and then stops before
claiming a complete result set. The Haringey smoke opens only the first page of
the rolling seven-day quick link and reports the source-provided totals. It
does not open application details or file links. Without `--confirm-live`,
both commands exit before constructing a live session.

Dorset has a dated, fixed-scope qualification command rather than a smoke:

```sh
uv run python scripts/qualify_dorset.py \
  --confirm-live --include-open \
  --data-dir .yimby/qualification-dorset-2026-09-16
```

The command fixes the inclusive received window at 18 August through 16
September 2026 and requires the complete outstanding query. It uses one HTTP
attempt per request, inherits the normal two-second host gap, persists a typed
v1 receipt atomically, and performs an immediate terminal rerun that must make
zero network requests. Reuse the same directory with `--resume` after a detail
or transport failure. If the official same-day result ordering invalidates a
nonterminal page checkpoint, add `--restart-discovery`. That explicit option
preserves runs, evidence, queued identities, and observations while replacing
only the stale query checkpoint. Terminal reference agreement still rejects a
stale queued identity. Neither option satisfies the weekly-cycle requirement.

Attachment bodies are outside policy. The transport blocks known attachment
paths, download endpoints, and image or media browser subresources before a
request. It rejects attachment media types or content dispositions before
consuming a response body. Adapters retain only document metadata and source
links.

## Inspect, rebuild, and export

```sh
uv run yimby --data-dir .yimby inspect <application-id>
uv run yimby --data-dir .yimby normalise --rebuild
uv run yimby --data-dir .yimby export --format jsonl --profile research
uv run yimby --data-dir .yimby export --format csv --profile public
uv run yimby --data-dir .yimby export --format parquet --profile public
```

`normalise --rebuild` reads retained native payloads and makes no authority
requests. Public exports use an explicit field allowlist. They exclude party
fields, officer names, comment bodies, native payloads, evidence bodies, and
unrestricted history. Suppression corrections apply before export. Every
public export includes a source-reuse reminder so publication is not mistaken
for permission to ignore the source register's terms.

## Dashboard

```sh
uv run yimby --data-dir .yimby dashboard
```

This starts a local Streamlit process. The dashboard shows the fixed coverage
denominator, package and live readiness, freshness, failures, backlog, request
and byte costs, duration, browser time, storage growth, application search,
observed source changes, a WGS84 map, and unmapped counts. The change count
includes removals and reversions but excludes normaliser-only rebuilds. Use
`dashboard --json` for a non-interactive snapshot suitable for tests and
scripts.

## Backup and restore

```sh
uv run yimby --data-dir .yimby backup
uv run yimby --data-dir .yimby backup --output /path/to/yimby-backup
uv run yimby restore /path/to/yimby-backup --target /path/to/new-data-dir
```

Backup creates a verified directory containing the SQLite snapshot, evidence,
and a hashed manifest. Restore verifies that directory and refuses an existing
target directory, so it cannot silently overwrite local data.

## Scheduling

Disabled examples are in `examples/launchd` and `examples/systemd`. Copy and
edit one only after the chosen authority is live-ready and manual bootstrap has
succeeded. The repository does not enable unattended execution.

The pilot is not accepted until every authority has completed live bootstrap
and two later weekly refreshes, approximately seven and fourteen days after the
bootstrap. Same-day reruns and simulated dates do not satisfy that requirement.
