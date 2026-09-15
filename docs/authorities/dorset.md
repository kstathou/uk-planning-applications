# Dorset portal walkthrough

Observed on 15 September 2026.

## Source

- Portal: `https://gi.dorsetcouncil.gov.uk/dorsetexplorer/planning/public`
- Shape: Dorset Explorer map client

## Observed response

After declining optional cookies and closing the introductory tour, the JavaScript client exposed a county map, background layers, legends, sharing, export, and a configurable search control.

The search configuration listed planning applications as an enabled provider that stops the search when it finds a match. It also listed addresses, roads, parishes, settlements, UPRNs, several coordinate formats, and other map providers. The client accepted British National Grid, latitude and longitude, and other coordinate searches.

No known application reference was supplied by the page, and this bounded walkthrough did not guess references or issue a broad unbounded map search. Application detail, documents, comments, enumeration, and pagination therefore remain unresolved.

## Collection consequences

- Treat the initial HTML and loading shell as incomplete. Capture the public JavaScript requests that populate planning search results.
- Keep planning-application results separate from address, road, parish, and coordinate providers.
- Preserve both map coordinates and application identifiers in the native schema.
- Use an independently bounded date or change feed for discovery. A map search alone is not a complete register enumeration.
- The fixture package may encode the client boundary, but it cannot be described as live application agreement until a result and detail route are verified.

## Verification status

`VERIFIED` for the loaded map client and planning search-provider configuration. Application discovery, detail extraction, completeness, and incremental refresh remain `INCONCLUSIVE`.
