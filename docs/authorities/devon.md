# Devon County Council portal walkthrough

Walkthrough date: 15 September 2026.

## Source

- Planning register: `https://planning.devon.gov.uk/`

The register covers minerals, waste, and county council development. It presents a copyright and data-use disclaimer before each requested route in the observed browser session.

## Discovery

The register exposes direct searches for applications received or decided during the past 7 or 90 days. The observed received-within-7-days search returned no records. The received-within-90-days search returned seven records.

Each result exposed a reference, application type, location, proposal, decision, and decision date. A zero-result page displayed an explicit no-records message. The scraper must distinguish that page from the disclaimer and from a failed request.

## Application record

Application `DCC/4473/2026` exposed type, case officer, received date, validation date, status, proposal, location, consultation deadline, decision fields, applicant, agent, addresses, district, electoral division, parish, and local member.

The page organised map, documents, constraints, and consultees as client-side tabs. Their content and document links were already present in the returned document. The observed document URLs used `/Document/Download` with record, plan, image, media-type, and filename parameters.

Public comments and consultee responses appeared as document metadata. The walkthrough did not open those files.

## Completeness rules

The scraper must accept the disclaimer through its session before every protected route when the server requests it. A disclaimer response is not an empty search or an application record.

The detail parser must read hidden tab content from the complete HTML instead of clicking every tab. Attachment bodies remain blocked even though the index is present in the page.

## Known limits

This walkthrough covered received-date quick searches and one current record. It did not inspect advanced search caps, decided results, pagination, comment submission, retries, or incremental changes.

## Request contract capture

The received-within-90-days route was rechecked on 16 September 2026. A
protected request redirects to `/Disclaimer?returnUrl=...`; the acceptance form
posts to `/Disclaimer/Accept?returnUrl=...` and then returns to the requested
route. The collector must recognise this intermediate page for each protected
request and must never parse it as an empty result.

The received search uses
`/Search/Standard?searchType=Received&days=90`. Its result document contains one
`dl.searchResultsList` per record and links references to
`/Planning/Display/<reference>`. Seven rows were again present. Each row exposed
the application number, application type, location, proposal, decision, and
decision date.

The observed detail route `/Planning/Display/DCC/4473/2026` contained two
`dl.details-grid` blocks for the core record and geography. Map, associated
documents, constraints, and consultees were present in the same response.
Document metadata links used `/Document/Download` with module, record number,
plan identifier, image identifier, plan flag, and filename parameters. The
capture enumerated link metadata only and did not retrieve an attachment body.
