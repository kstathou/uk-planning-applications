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

Arun and Devon restrict discovery to their recorded received-date paths.
Peak District's smoke remains a small rolling-week diagnostic, while its
complete bootstrap uses the qualification command below. Camden resolves one
explicit reference and deliberately rejects an unsupported bounded date
search. These commands refuse before creating a live session unless
`--confirm-live` is present.

Peak District has a dedicated persisted qualification command:

```sh
uv run python scripts/qualify_peak_district.py \
  --confirm-live \
  --data-dir .yimby/qualification-peak-district-2026-09-16 \
  --start 2026-08-18 \
  --end 2026-09-16 \
  --include-open
```

The interval must contain exactly 30 inclusive days and `--include-open` is
mandatory. The command submits bounded Received, Validated, and Decided queries
plus any-time REGISTERED and APPEAL LODGED queries. It writes a versioned JSON
private proof before its local published receipt, and only after checkpoint and
query inventory, reference and application
agreement, retry state, failed sections, database integrity, evidence paths and
digests, persisted live-ready HTTP metadata, unmapped records, attachment
policy, cumulative authority-scoped durable run-cost agreement, and an
immediate zero-network rerun all pass. A valid resume recovers the existing
proof without opening a source session; missing or inconsistent terminal proof
fails closed. Use `--resume` only with the same directory and exact scope after
an interruption or fail-closed correction.

The accepted 16 September 2026 receipt records 377 references and applications,
95 decision dates, zero failed sections, zero pending or historical retry
entries, and zero attachment-body requests. It validates all 1,560 retained
evidence rows. Peak District is `LIVE_READY` for the receipt-backed HTTP
contract, but remains operationally unqualified until successful weekly cycles
occur on or after 23 September and 30 September 2026.

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

OPDC has a persisted qualification command rather than a discovery-only smoke:

```sh
uv run python scripts/qualify_opdc.py \
  --confirm-live \
  --data-dir .yimby/qualification-opdc-2026-09-16 \
  --end 2026-09-16 \
  --include-open
```

The command derives the inclusive 30-day start date, refuses a non-empty target
without `--resume`, and rejects a changed scope in an existing qualification
store before opening a network session. It writes
`opdc-qualification-proof-v1.json` before it publishes
`opdc-qualification-v1.json`. The private proof records the cumulative
bootstrap request and byte cost plus the complete identity inventory. A
terminal `--resume` validates that cost against the authority's durable run
rows and checks the proof against the current store. It performs no source
requests. If public receipt publication fails, the next resume republishes the
validated proof. A terminal store without a valid private or legacy public
proof fails with `bootstrap-provenance` instead of recording a zero-cost
bootstrap.

The command creates the proof only after the exact query inventory, reference
agreement, complete application evidence, SQLite integrity, zero retry and
failure counts, evidence digest verification, exact per-application capture URL
associations, attachment policy, and immediate zero-network rerun all pass.
Its data directory contains the SQLite store and compressed source evidence.
The command never requests document bodies. The
[sanitized committed receipt](evidence/opdc-qualification-2026-09-16.json)
keeps the aggregate proof reviewable without the ignored local store or its
55-row public identity inventory. It records 103 distinct content digests, 165
ordered application-to-capture associations, and SHA-256 commitments to both
the content-digest set and the exact per-application capture associations.

Blackburn with Darwen has an authority-specific persisted qualification command:

```sh
uv run python scripts/qualify_blackburn_with_darwen.py \
  --confirm-live \
  --data-dir .yimby/qualification-blackburn-with-darwen-2026-09-16 \
  --start 2026-08-18 \
  --end 2026-09-16 \
  --include-open \
  --resume
```

The command accepts exactly one inclusive 30-day window and requires
`--include-open`. Omit `--resume` only for a new or empty directory. It searches
received, valid, and decision dates for the recent window and received dates
from 1 January 1977 for older open cases. Capped ranges split recursively. A
30-row result on a single day, form drift, route drift, or incomplete store
fails closed.

The source requires visible Chromium and may present an attended human check.
After that check is completed, its browser storage state may be saved as
`browser-state.json` inside the qualification directory. The command reuses
that file automatically. It can contain a short-lived verification token, so
keep it local, ignored, and unshared.

Every attempt atomically replaces
`blackburn-with-darwen-qualification-v1.json` with either a typed `qualified`
or `blocked` result. Qualification requires a terminal coherent query tree,
zero pending retries, zero failed sections, database and evidence integrity,
one persisted application per discovered reference, zero attachment-body
requests, and an immediate rerun with zero requests, bytes, and browser time.
The initial receipt cost aggregates every durable run in the fixed scope before
that immediate rerun, including interrupted attempts.
The receipt keeps the later 23 and 30 September cycles pending. Same-day replay
does not satisfy them.

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
bootstrap. OPDC completed its bootstrap on 16 September 2026; its later cycles
remain pending. Same-day reruns and simulated dates do not satisfy that
requirement.
