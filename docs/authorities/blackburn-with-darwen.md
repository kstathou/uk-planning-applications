# Blackburn with Darwen portal walkthrough

Observed on 15 and 16 September 2026.

## Source

- Council planning-search page links to `https://online.blackburn.gov.uk/planning/`.
- Current portal shape: Blackburn-owned Citizen register.
- Legacy portal: `https://planning.blackburn.gov.uk/Northgate/PlanningExplorer/`.

The legacy Northgate entry point still returns a maintenance page. It is not the
current source linked by the council. The current Citizen portal exposes search,
application detail, and inline document metadata through the official
`online.blackburn.gov.uk` host.

## Search contract

The current search form submits `POST /planning/index.html`. The rendered form
has exposed two equivalent hidden discriminator pairs during the walkthrough:
empty `fa` and `submitted` values, and `fa=search` with `submitted=true`. The
adapter preserves the portal-owned values and rejects any other pair.

Date inputs use inclusive `DD-MM-YYYY` values. The supported searches are:

- `received_date_from` and `received_date_to`
- `valid_date_from` and `valid_date_to`
- `decision_issued_date_from` and `decision_issued_date_to`

A 30-day search returned exactly 30 rows without a total or pagination control.
The generic latest-applications page also returned 30 rows and included decided
cases, so it cannot prove a complete open inventory. The adapter treats 30 as
an observed cap. It recursively bisects capped date ranges and fails closed if
a single day still returns 30 rows.

The council describes 1977 as the public register's lower bound. For
`include_open`, the adapter searches received dates from 1 January 1977 through
the day before the requested recent window and retains rows whose public
decision is empty. Recent received, valid, and decision searches cover the
exact requested 30-day window.

## Detail and child sections

The direct detail route is
`/planning/index.html?fa=getApplication&id=<numeric-record-id>`. Record `178041`,
public reference `10/26/0747`, exposed the labelled application fields and an
eight-row inline document table during the browser walkthrough. Document rows
publish type, description, added date, and a source link. The collector retains
that metadata and never activates a document link.

No public comment section or comment count was present on the current detail
surface. Comments are therefore marked unavailable rather than complete and
empty. The grid reference is retained as a native field. Application details,
documents, and comment availability each have explicit completeness state.

## Browser and verification boundary

Direct HTTP requests are rejected by the source firewall. Headless Chromium
received HTTP 403 with `IDX005`. Visible Chromium received a JavaScript
challenge before exposing the search form. Direct detail navigation then
presented an explicit human-verification check.

The authority uses a visible, single-worker Playwright session. Every primary
navigation and form submission waits at least two seconds. The shared browser
boundary blocks attachment paths plus image and media subresources. A completed
human check may be stored in the ignored qualification directory as
`browser-state.json`; the qualification command can reuse it, but the file is
local session state and must never be committed or shared.

## Qualification status

The persisted 16 September attempt is currently fail-closed with
`BlackburnHumanVerificationRequiredError`. Before the detail check stopped the
run, the store durably queued 26 discovered references. It contains zero
applications and two retry items, so it does not prove a live bootstrap.

The typed receipt is
`.yimby/qualification-blackburn-with-darwen-2026-09-16/blackburn-with-darwen-qualification-v1.json`.
It records the exact 18 August through 16 September scope, `include_open=true`,
the blocker, and pending weekly cycles on 23 and 30 September. Readiness remains
`browser-only` until a resumed run completes every query, every detail, all
integrity checks, and an immediate zero-network replay.

## Verification status

The current portal, exact form fields, date format, observed cap, detail route,
document metadata, absent comment surface, and browser challenge are verified.
Same-day live completeness is blocked pending an attended verification state.
The two later weekly cycles remain pending.
