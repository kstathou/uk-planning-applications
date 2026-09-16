# Old Oak and Park Royal Development Corporation portal walkthrough

Observed and qualified on 16 September 2026.

## Sources

- Citizen Portal: `https://planning.agileapplications.co.uk/opdc`
- Public planning API: `https://planningapi.agileapplications.co.uk`
- Portal family: Agile Applications Citizen Portal

The public API requires the same fixed routing headers used by the official
browser client: `x-client: OPDC`, `x-product: CITIZENPORTAL`, and
`x-service: PA`. They are public product selectors, not credentials.

This API is the preferred machine-readable source required by the pilot plan.
It supplies the complete search arrays, stable Agile identifiers, application
records, document metadata, and public response text used by the collector, so
the implementation does not scrape the rendered Citizen Portal. A source audit
on 16 September 2026 found no more complete OPDC application dump. The national
[Planning application dataset](https://www.planning.data.gov.uk/dataset/planning-application)
listed no data providers and reported that its collector last ran on 17
September 2025, so it cannot prove OPDC's current 30-day or older-open inventory.

## Discovery contract

The rendered portal exposes Registered and Determined searches. Its result
pager slices a complete API array in the browser; it does not issue server-side
page requests. The adapter therefore makes three exact requests and accepts a
query only when the API's `total` equals the number of returned rows:

| Purpose | Exact parameters |
|---|---|
| Bounded registered | `registrationDateFrom=2026-08-18&registrationDateTo=2026-09-16&status=registered` |
| Bounded determined | `decisionDateFrom=2026-08-18&decisionDateTo=2026-09-16&status=determined` |
| Complete current registered/open set | `status=registered` |

The inclusive 30-day qualification returned 10 registered rows, 10 determined
rows, and 45 current registered rows. Their stable reference/application-ID
union contained 55 applications. Overlap is accepted only when both the public
reference and Agile ID agree. Duplicate or conflicting identities, a false
total, an altered query order, or a partial terminal checkpoint fails closed.

The unbounded `status=registered` request is the older-open strategy because it
is the official portal's complete current Registered surface. The adapter does
not invent a date partition or exclude records whose references look like test
data.

## Application sections

Every discovered reference is routed by its Agile application ID and checked
against both the returned ID and public reference. The adapter retrieves:

- `/api/application/{id}` for the application record;
- `/api/application/{id}/document` for the complete document metadata array;
- `/api/application/{id}/responses` for the complete public response-text
  array.

The application model retains the proposal, status, site, application type,
decision, dates, alternative reference, ward, and British National Grid
coordinates used by the common schema. Document rows retain metadata and a
derived attachment URL. Normalisation maps the API's document media description
to the common category and its received date to the common published date.
Public response text is retained as comments.

An empty JSON array is recorded as an empty section. A transport or parse
failure is recorded as failed, never empty. The qualification receipt is
withheld while any current section is failed.

## Attachment policy

Document bodies are outside the pilot. The metadata endpoint is allowed, but
`/api/application/document/OPDC/{documentId}` is blocked before network access
by the fixture, HTTP, and browser transports. The qualified run made zero
attachment-body requests.

## Persisted qualification

The full local bootstrap is stored in
`.yimby/qualification-opdc-2026-09-16/`. Its private qualification proof keeps
the cumulative bootstrap request and byte cost plus the full identity
inventory. A terminal resume validates that cost against durable run rows and
checks the proof against the current store without source requests. The
[committed receipt](../evidence/opdc-qualification-2026-09-16.json) is
sanitized. It retains the scope, query totals, aggregate counts, costs, run
statuses, checks, and evidence commitments while omitting the 55-row identity
inventory from the repository.

The receipt records:

- 55 applications and 55 discovered references.
- 55 native, application, document, and comment versions.
- Zero pending retries, failed sections, and unmapped records.
- SQLite integrity `ok`, 103 distinct content digests, 165 ordered application
  capture associations, no missing evidence paths, and no digest-invalid
  evidence.
- 168 allowed initial requests transferring 630,334 bytes.
- Zero attachment-body attempts.
- An unchanged immediate rerun with zero requests and zero transferred bytes.

Both collection runs completed with `succeeded` status. The terminal checkpoint
contains the exact three-query inventory, each declared total, and the same 55
source/reference/locator identities held by the durable queue and retained
applications. Each query's declared total is also tied to its retained identity
inventory. Every application rebuild input preserves its own ordered detail,
document-index, and response URL association even when response bodies have the
same digest. The sanitized receipt records SHA-256 commitments to the sorted
content-digest set and to the exact source identity plus ordered capture URL,
media type, and content digest associations. The application input is ordered
by source ID, reference, and locator while each application's capture order is
retained. The content-digest input is sorted. Both inputs use ASCII JSON with
sorted object keys and compact separators before hashing.
The persisted authority manifest is also checked as `live-ready` with HTTP
transport before the receipt can be written.

The current collector additionally retains each search response in
`discovery_evidence`, bound to its query name, page, response URL, request URL,
method, and ordered form values. Qualification requires one persisted run whose
three registered subjects and parsed identity arrays agree exactly with the
terminal checkpoint. Later 30-day windows may replace the terminal checkpoint
without deleting earlier discovery registrations or applications. A failed
later window leaves both previous receipt files unchanged.

## Verification status

`LIVE_READY` for the recorded HTTP contract. The live bootstrap and browser/API
agreement are verified. The 16 September receipt predates the new persisted
search-request binding and has not been regenerated by this code-only
remediation. Deterministic qualification tests cover that boundary, shifted
windows, cumulative retention, and failure-safe receipt replacement. A fresh
live receipt and the two operational refresh cycles approximately seven and
fourteen days later remain open; same-day reruns do not satisfy them.
