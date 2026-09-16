# Old Oak and Park Royal Development Corporation portal walkthrough

Observed and qualified on 16 September 2026.

## Sources

- Citizen Portal: `https://planning.agileapplications.co.uk/opdc`
- Public planning API: `https://planningapi.agileapplications.co.uk`
- Portal family: Agile Applications Citizen Portal

The public API requires the same fixed routing headers used by the official
browser client: `x-client: OPDC`, `x-product: CITIZENPORTAL`, and
`x-service: PA`. They are public product selectors, not credentials.

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
derived attachment URL. Public response text is retained as comments.

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
`.yimby/qualification-opdc-2026-09-16/`. A sanitized
[committed receipt](../evidence/opdc-qualification-2026-09-16.json) retains the
scope, query totals, aggregate counts, costs, run statuses, and checks while
omitting the 55-row identity inventory from the repository.

The receipt records:

- 55 applications and 55 discovered references;
- 55 native, application, document, and comment versions;
- zero pending retries, failed sections, and unmapped records;
- SQLite integrity `ok`, no missing evidence paths, and no digest-invalid
  evidence among 103 content-addressed captures;
- 168 allowed initial requests transferring 630,334 bytes;
- zero attachment-body attempts; and
- an unchanged immediate rerun with zero requests and zero transferred bytes.

Both collection runs completed with `succeeded` status. The terminal checkpoint
contains the exact three-query inventory, each declared total, and the same 55
source/reference/locator identities held by the durable queue and retained
applications. Each query's declared total is also tied to its retained identity
inventory. Every application rebuild input preserves its own ordered detail,
document-index, and response URL association even when response bodies have the
same digest.
The persisted authority manifest is also checked as `live-ready` with HTTP
transport before the receipt can be written.

## Verification status

`LIVE_READY` for the recorded HTTP contract. The live bootstrap and browser/API
agreement are verified. The two operational refresh cycles approximately seven
and fourteen days later remain open; same-day reruns do not satisfy them.
