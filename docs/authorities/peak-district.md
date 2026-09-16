# Peak District portal walkthrough

Walkthrough date: 15 September 2026.

## Sources

- Legacy information portal: `https://portal.peakdistrict.gov.uk/`
- AssureLive replacement: `https://planning.peakdistrict.gov.uk/AssureLive/`

The legacy portal remains the comment route. It links each current record to the replacement for the latest record and documents. The authority warns that some legacy document lists for 2025 are incomplete.

## Discovery

The legacy portal has direct quick searches for applications and appeals validated during the past week or month. The observed weekly application query returned 33 records and displayed 10 rows per page.

The result table used client-side pagination. Each row contained a reference, address, record type, proposal, and date. The application reference is encoded in an opaque result URL and also appears as readable text.

## Application record

Application `NP/DIS/0926/0917` exposed a planning-portal reference, status, type, address, parish, validated date, target decision date, and legal-agreement flag. The page linked directly to the matching AssureLive record.

Two additional sections remained in a loading state during the initial render. The legacy client loads schema-specific data with JavaScript. A successful summary response does not prove that those sections loaded.

## Completeness rules

The adapter must record the legacy and replacement portals as separate dated sources. It must prefer the replacement for document completeness while retaining the legacy route for comments and historical records.

The adapter must distinguish a visible loading state from an empty section. Client-side pagination must enumerate all 33 rows from the observed query, not only the first 10 displayed rows.

## Known limits

This walkthrough covered one weekly query and one current record. It did not enumerate the JavaScript-loaded sections, replacement-portal documents, comments, decided cases, older open applications, or incremental changes.

## Request contract capture

The legacy weekly route was rechecked on 16 September 2026 at
`/quicksearch/validated_past_week`. The returned `#searchresults` table was
enhanced by a client-side DataTable. At that time it reported 30 entries over
three pages, displayed ten rows at once, and provided the readable reference,
record type, description, date, and one opaque `/result/...` link per record.
The collector must parse or enumerate all entries rather than treat the first
ten DOM rows as the complete result.

The observed result page exposed the reference, description, planning portal
reference, status, application type, development address, and parish. Four
other areas still rendered `Loading...`, and no successful child-data request
was visible in the document. Those sections remain failed or partial, not
empty. The replacement AssureLive system remains the required source for
current documents and other migrated fields.
