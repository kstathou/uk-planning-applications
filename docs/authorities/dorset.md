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
clicked search control in native form order. The outstanding checkbox must be
present, enabled, and typed as a checkbox; the adapter will not fabricate it.
Every underlying HTTP hop, including automatic disclaimer redirects, receives
its own Dorset-local two-second host turn. Before dispatch, each hop must use
HTTPS, the exact official host and port, and one of the four register routes
used by discovery and detail collection. Redirect destinations are checked
before they can be followed, and every hop's response headers are checked for
attachment metadata before its body is exposed to HTTPX. Pagination posts the
captured upper `NextButton`, whose value is one space, along with the result
form's current view state. The terminal result page still renders enabled
next-button chrome. The two agreeing `Page n of n` markers establish
terminality, and the adapter never submits a next request from a terminal page.

Search pages are scanned atomically before detail work is released. Each
nonterminal page commits an empty work batch plus typed checkpoint proof. The
terminal page releases the complete query reference set. Cross-page duplicate
references, changed page counts, replay mismatches, short nonterminal pages,
and conflicting locators all fail closed. `--restart-discovery` restarts only
an untrusted active outstanding query when the received query is already
complete; a superseded terminal inventory is fully rescanned. Both paths
preserve runs, evidence, queued identities, and observations. Final reference
agreement proves every terminal identity against the queue and application
table while retaining formerly in-scope applications as historical records.

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

- 49 received-date pages and 136 unrestricted outstanding pages exhausted.
- 489 received-date identities plus 949 additional older-open identities.
- 1,438 current applications, queued references, and terminal-checkpoint
  references with
  the same SHA-256 identity-set hash,
  `c7a723dd58efcc5ce8f29472097f76a49d3ebc59e878412fb1fa7ef4cba25162`.
- 1,445 retained application evidence records and 1,445 unique evidence
  digests, all decompressed and re-hashed successfully. Seven retained
  historical applications are outside the terminal inventory.
- Successful live source run `02645b74-153a-4e81-82af-577502f15bb8` made 1,629
  single-attempt fetches, transferred 175,352,179 bytes, and made zero
  attachment-body requests.
- Zero pending retries, failed sections, and unmapped records.
- A second successful terminal run with zero fetches, zero transferred bytes,
  and no database or evidence change.
- Receipt SHA-256
  `bcdc3d7f000d601d39b87c1aceccb5f05ad767e61455495063efd4114ffa5269`.

An earlier receipt was superseded after independent review showed that its
outstanding query had inherited the bounded received dates. The corrected
unrestricted query advertised 136 pages rather than 40. Historical failed runs
and live-derived parser stops remain in the same store as audit evidence.
A second independent review then found that redirect hops were not individually
rate-limited and that a missing outstanding checkbox could be synthesized.
Both paths now fail closed or remain individually metered. During the corrected
rescan, the live outstanding set changed from 135 to 136 pages; one contradicted
pass failed closed and the explicit query-local restart completed the stable
136-page scan. The receipt cites the successful nonzero live source run, selects
two successful zero-network validation runs, and contains no failed check.
A final adversarial review found that automatic redirects could still leave the
official origin and that HTTPX could consume an attachment-marked intermediate
redirect body. Exact pre-dispatch route validation and pre-body response-header
checks now close both paths. A bounded official smoke of the corrected transport
completed the disclaimer flow, but independent review correctly rejected that
smoke as insufficiently bound to the typed receipt. The full fixed-window and
outstanding qualification was therefore restarted through the hardened
transport. Its cited live source run now supplies the receipt's nonzero costs
and zero attachment count directly. A later validation-only resume must match
the prior typed receipt's source run, request count, byte count, and attachment
count to durable run metrics; it cannot synthesize historical attachment
accounting.
A subsequent independent review found the inverse form-state risk: a prechecked
outstanding control could narrow the received-date request. The adapter now
requires that published checkbox to be present, enabled, typed correctly, and
initially unchecked before either query is built. The review also exposed an
impossible 48-page documentation claim; the official received query is 49
pages. The complete qualification was restarted again after that fix, and the
receipt above cites that final source run rather than the earlier 1,437-identity
run.

## Readiness

Dorset is `discovery-only`, not `live-ready`. The same-day bootstrap and
zero-network terminal rerun passed. Weekly cycles due on 23 September 2026 and
30 September 2026 remain pending. Only those later live refreshes can establish
operational qualification.
