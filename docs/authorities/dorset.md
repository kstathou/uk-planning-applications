# Dorset portal walkthrough and qualification

Walkthrough and qualification date: 16 September 2026.

## Sources

- Statutory planning register: `https://planning.dorsetcouncil.gov.uk/`
- Explorer fixture boundary: `https://gi.dorsetcouncil.gov.uk/dorsetexplorer/planning/public`

The planning register is the live source. Dorset Explorer remains useful for
deterministic map fixtures, but it is not used for bounded live enumeration.

## Discovery contract

The register is an ASP.NET and Telerik application. A new session first returns
the official disclaimer form. The adapter replays its `__VIEWSTATE`,
`__VIEWSTATEGENERATOR`, and `__EVENTVALIDATION` values, accepts the disclaimer,
and reloads the advanced search in the same cookie session.

The exact live inventory is:

1. `received-valid`, with Date Received from 18 August 2026 through 16 September
   2026 inclusive.
2. `outstanding`, with the portal's outstanding-only checkbox and no date
   restriction.

The adapter submits both the ISO and visible Telerik date values, captured
numeric and date client state, the portal's active calendar values, and the
clicked search control in native form order. Pagination posts the captured
upper `NextButton`, whose value is one space, along with the result form's
current view state. The terminal result page still renders enabled next-button
chrome. The two agreeing `Page n of n` markers establish terminality, and the
adapter never submits a next request from a terminal page.

Search pages are scanned atomically before detail work is released. Each
nonterminal page commits an empty work batch plus typed checkpoint proof. The
terminal page releases the complete query reference set. Cross-page duplicate
references, changed page counts, replay mismatches, short nonterminal pages,
and conflicting locators all fail closed. `--restart-discovery` replaces a
same-scope query checkpoint, including a superseded terminal inventory, and
preserves runs, evidence, queued identities, and observations. Final reference
agreement rejects any stale durable record.

## Detail and section contract

Each detail request uses the numeric `recno` locator published by the result
page. The live native schema retains the application reference, status, type,
proposal, valid date, decision, authority, address, British National Grid
coordinates, ward, parish, document metadata, and source URL. A present but
blank ward or parish is retained as `None`. A source-provided pair of blank
coordinates becomes an absent location, while a partial pair fails closed. A
legacy blank proposal remains blank rather than receiving invented text.
Missing labels, address, or required identity, status, type, and date values
fail closed.

The document section is metadata-only. The Telerik grid's row count must agree
with its typed `VirtualItemCount`, `PageCount=1`, and `AllowPaging=false` proof.
The exact `rgNoRecords` message proves an empty grid. Duplicate visible document
metadata is distinguished by the official row index in a local fragment such
as `#document-7`. A source-provided blank document title remains `None`, while
its date, size, and row identity are retained. No `RowClicked` action is invoked
and no attachment body is requested. Dorset does not expose public comment text
in this register, so the comments section is explicitly unavailable rather
than empty.

## Dated qualification result

The opt-in command was:

```sh
uv run python scripts/qualify_dorset.py \
  --confirm-live --include-open --resume \
  --data-dir .yimby/qualification-dorset-2026-09-16
```

The typed v1 receipt is
`.yimby/qualification-dorset-2026-09-16/dorset-qualification-v1.json`.
It records:

- 48 received-date pages and 136 unrestricted outstanding pages exhausted.
- 474 received-date identities plus 956 additional older-open identities.
- 1,430 applications, queued references, and terminal-checkpoint references with
  the same SHA-256 identity-set hash,
  `a0eb643504ea2e98e4ed70d916b35666700a342875c0c52133e063a31b471d30`.
- 1,430 retained current native records and 1,430 unique evidence digests, all
  decompressed and re-hashed successfully.
- The final successful resume made 256 single-attempt fetches, transferred
  53,934,300 bytes, and made zero attachment-body requests.
- Zero pending retries, failed sections, and unmapped records.
- A second successful terminal run with zero fetches, zero transferred bytes,
  and no database or evidence change.
- Receipt SHA-256
  `27028882645b8cf760613f75349bfcf24d7735f258bc808ade1741e89dfa3778`.

An earlier receipt was superseded after independent review showed that its
outstanding query had inherited the bounded received dates. The corrected
unrestricted query advertised 136 pages rather than 40. Historical failed runs
and live-derived parser stops remain in the same store as audit evidence. The
receipt selects the final two successful runs and contains no failed check.

## Readiness

Dorset is `discovery-only`, not `live-ready`. The same-day bootstrap and
zero-network terminal rerun passed. Weekly cycles due on 23 September 2026 and
30 September 2026 remain pending. Only those later live refreshes can establish
operational qualification.
