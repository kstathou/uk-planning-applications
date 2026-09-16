# Blackburn with Darwen TODO

Checkpoint recorded on 16 September 2026. Preserve the existing adapter,
tests, audit trail, ignored qualification directory, and typed receipt.

## What works

- The authority-owned Citizen adapter implements exact received, valid, and
  decision searches for an inclusive 30-day window.
- Older-open discovery searches received dates from 1 January 1977 to the day
  before the recent window and retains rows with no public decision.
- The observed 30-row portal cap is handled by recursive date-range splitting.
  A capped single day fails closed.
- Detail parsing, grid coordinates, inline document metadata, typed
  checkpoints, retries, and terminal zero-network replay are implemented.
- Attachment links are never activated. No attachment bodies have been
  requested. Public comments remain explicitly unsupported.
- Visible Chromium uses two-second pacing, one worker, local storage-state
  reuse, resource blocking, and lock-protected receipts.
- The last complete repository gate run passed 385 tests with 100 percent
  statement and branch coverage. Ruff, configured mypy, package builds, and
  `git diff --check` also passed.

## API-first findings

No official machine-readable source currently proves the required current
30-day plus older-open inventory.

- The council's Citizen map calls
  `/planning/index.html?fa=search_map_applications`. The endpoint is protected
  by the same challenge as the portal. Its client enforces a 7,500-record
  warning and exposes only record id, reference, proposal, address, and
  geometry. It exposes no received date, valid date, decision date, status,
  total count, or freshness marker.
- The council-linked StatMap guest service at
  `https://blackburn.statmap.co.uk/map/Aurora.svc` exposes `Planning Apps from
  2020` and `Planning Apps 1977_2019` layers. Its session declares a 100-result
  maximum. The public interface supports point queries, not a complete dated
  export, and exposes no total-count proof. A query at the published coordinate
  for current record `10/26/0747` returned no application record.
- The legacy Northgate portal still returns its maintenance page.
- The national Planning Data Platform does not provide an authoritative,
  current Blackburn planning-application feed.

Do not replace the Citizen collector with either map source unless the council
publishes a dated, complete export or an API with stable identifiers, freshness,
pagination or total counts, and fields sufficient to distinguish recent and
older-open applications.

## Exact persisted state

The live result is not qualified. Registry readiness remains `browser-only`
with browser transport. Discovery, document metadata, and coordinates are
supported. Comments are unsupported.

The ignored data directory is
`.yimby/qualification-blackburn-with-darwen-2026-09-16`.

- Receipt outcome is `blocked`.
- Receipt creation time is `2026-09-16T16:03:10.546119Z`.
- Blocker is `BlackburnHumanVerificationRequiredError`.
- Scope is 18 August through 16 September 2026 with `include_open=true`.
- Weekly cycles for 23 and 30 September 2026 remain pending.
- SQLite integrity is `ok`.
- The store contains 26 discovered references, one application, one native
  version, one application version, one document version, and two pending
  retries.
- Pending reference `10/26/0735` has three attempts and the human-verification
  blocker. Pending reference `10/26/0756` has one timeout attempt.
- `browser-state.json` is the last preserved input state. Failed interactions
  did not overwrite it.
- `browser-state-attended.json` contains the later challenge-page state only.
  It is mode `0600` and must not be promoted.

## Remaining work

1. Complete the portal's human-verification step in an attended visible
   browser session.
2. Confirm that the known detail page is visible before saving or promoting a
   new browser state.
3. Resume the formal qualification until every discovery query and detail is
   complete, both pending retries clear, and the immediate replay makes zero
   network requests.
4. Keep readiness fail-closed if any completeness check remains unproved.
5. Run the full gates and obtain a fresh independent exact-head review. Accept
   only the exact response `NO COMMENTS`.

## Safe resume commands

Run these from the repository root. Save attended state to a new candidate so
the preserved input state cannot be overwritten by another challenge page.

```sh
blackburn_data_dir=.yimby/qualification-blackburn-with-darwen-2026-09-16

uv run playwright open -b chromium \
  --load-storage="$blackburn_data_dir/browser-state.json" \
  --save-storage="$blackburn_data_dir/browser-state-next.json" \
  'https://online.blackburn.gov.uk/planning/index.html?fa=getApplication&id=178041'
```

Complete verification in the visible browser. Confirm that application
`10/26/0747` is visible, then close the browser so Playwright saves the
candidate. Do not promote a candidate that still opens the human-verification
page.

After visual confirmation, preserve the prior state and promote the new one.

```sh
blackburn_data_dir=.yimby/qualification-blackburn-with-darwen-2026-09-16

mv "$blackburn_data_dir/browser-state.json" \
  "$blackburn_data_dir/browser-state.pre-attended.json"
mv "$blackburn_data_dir/browser-state-next.json" \
  "$blackburn_data_dir/browser-state.json"
chmod 600 "$blackburn_data_dir/browser-state.json" \
  "$blackburn_data_dir/browser-state.pre-attended.json"
```

Resume the exact qualification scope.

```sh
uv run python scripts/qualify_blackburn_with_darwen.py \
  --confirm-live \
  --data-dir .yimby/qualification-blackburn-with-darwen-2026-09-16 \
  --start 2026-08-18 \
  --end 2026-09-16 \
  --include-open \
  --resume
```

Finally run the repository gates.

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
git diff --check
git status --short
```
