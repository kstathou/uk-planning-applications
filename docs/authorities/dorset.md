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
and conflicting locators all fail closed. `--restart-discovery` replaces only a
stale nonterminal checkpoint and preserves runs, evidence, queued identities,
and observations. Final reference agreement rejects any stale durable record.

## Detail and section contract

Each detail request uses the numeric `recno` locator published by the result
page. The live native schema retains the application reference, status, type,
proposal, valid date, decision, authority, address, British National Grid
coordinates, ward, parish, document metadata, and source URL. A present but
blank ward or parish is retained as `None`; missing labels, address, coordinates,
or required application values fail closed.

The document section is metadata-only. The Telerik grid's row count must agree
with its typed `VirtualItemCount`, `PageCount=1`, and `AllowPaging=false` proof.
The exact `rgNoRecords` message proves an empty grid. Duplicate visible document
metadata is distinguished by the official row index in a local fragment such
as `#document-7`. No `RowClicked` action is invoked and no attachment body is
requested. Dorset does not expose public comment text in this register, so the
comments section is explicitly unavailable rather than empty.

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

- 48 received-date pages and 40 outstanding pages exhausted.
- 472 received-date identities plus one additional older-open identity.
- 473 applications, queued references, and terminal-checkpoint references with
  the same SHA-256 identity-set hash,
  `8c92c330cc5afe5377dbe04703f45400fe016418d02bead90bddcc66368a7684`.
- 473 retained native records and 473 unique evidence digests, all decompressed
  and re-hashed successfully.
- 565 successful single-attempt fetches, 47,284,894 transferred bytes, and zero
  attachment-body requests.
- Zero pending retries, failed sections, and unmapped records.
- A second successful terminal run with zero fetches, zero transferred bytes,
  and no database or evidence change.

Historical failed runs remain in the same store as audit evidence. The receipt
selects the final two successful runs and contains no failed check.

## Readiness

Dorset is `discovery-only`, not `live-ready`. The same-day bootstrap and
zero-network terminal rerun passed. Weekly cycles due on 23 September 2026 and
30 September 2026 remain pending. Only those later live refreshes can establish
operational qualification.
