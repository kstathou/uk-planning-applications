# Pilot portal inventory

This reference records the browser census taken on 15–16 September 2026. A successful entry-page check does not prove extraction completeness. Each authority still needs detail-page, pagination, failure, and incremental-refresh checks before live verification can pass.

| Authority | Observed sources | Portal shape | Browser result |
|---|---|---|---|
| Barnet | `publicaccess.barnet.gov.uk/online-applications/` | IDOX Public Access | Weekly discovery and one detail record verified. See `authorities/barnet.md`. |
| Camden | Official Socrata Planning Applications dataset `2eiu-s2cw` | HTTP JSON API; historical portal sources retained offline | Primary metadata feed verified live; documents and comment text unsupported. See `authorities/camden.md`. |
| Haringey | `londonboroughofharingey.my.site.com/pr/s/` | Salesforce public register | Seven-day discovery, one detail record, one explicit empty comment section, and its six-row file index were verified. See `authorities/haringey.md`. |
| Devon County Council | `planning.devon.gov.uk/` | Custom register | The exact advanced form, dated planning and appeal searches, six-page outstanding planning and two-page outstanding appeal searches, both detail routes, and metadata-only documents were verified. A persisted bootstrap produced 67 unique records. See `authorities/devon.md`. |
| Peak District National Park Authority | AssureLive current portal plus legacy information portal | Two linked services | Exact 30-day Received, Validated, and Decided queries plus any-time REGISTERED and APPEAL LODGED were exhausted. All 377 unique details and document-metadata indexes were persisted without attachment bodies. See `authorities/peak-district.md`. |
| Arun | `www1.arun.gov.uk/aplanning/OcellaWeb/` | Ocella | One decided detail record and its document index were verified without opening attachment bodies. See `authorities/arun.md`. |
| Old Oak and Park Royal Development Corporation | Citizen Portal plus `planningapi.agileapplications.co.uk` | Agile Applications | Exact bounded Registered and Determined searches, the complete current Registered set, detail, document metadata, and public responses were verified and qualified. See `authorities/opdc.md`. |
| Dorset | `gi.dorsetcouncil.gov.uk/dorsetexplorer/planning/public` | Dorset Explorer map client | The JavaScript map and planning search-provider configuration loaded, but no bounded application enumeration was exposed. See `authorities/dorset.md`. |
| Cheshire East | `pa.cheshireeast.gov.uk/planning/index.html?fa=search` | Custom register | A valid-date search and its field inventory were verified. A selected result did not yield readable detail content. See `authorities/cheshire-east.md`. |
| Blackburn with Darwen | `planning.blackburn.gov.uk/Northgate/PlanningExplorer/` | Northgate Planning Explorer | Two checks returned the same maintenance page. See `authorities/blackburn-with-darwen.md`. |
| Birmingham | `eplanning.birmingham.gov.uk/Northgate/PlanningExplorer/` | Northgate Planning Explorer | Two checks returned HTTP 503. See `authorities/birmingham.md`. |
| Leeds | `publicaccess.leeds.gov.uk/online-applications/` | IDOX Public Access | Weekly discovery returned explicit zero and 156-record outcomes for adjacent weeks. The selected detail returned a remote exception. See `authorities/leeds.md`. |
| Cornwall | `planning.cornwall.gov.uk/online-applications/` | IDOX Public Access | Weekly discovery, one current detail, explicit section counts, and its complete 16-row document index were verified without opening attachment bodies. See `authorities/cornwall.md`. |
| Durham County Council | `publicaccess.durham.gov.uk/online-applications/` | IDOX Public Access | Weekly discovery, one detail record, the comment-section shape, and its six-row document index were verified without opening attachment bodies. See `authorities/durham.md`. |
| West Suffolk | `planning.westsuffolk.gov.uk/online-applications/` | IDOX Public Access | Weekly discovery, one detail record, and its six-row document index were verified without opening attachment bodies. See `authorities/west-suffolk.md`. |

## Source rules found during the census

The registry must store sources separately from authorities. Camden and Peak District already require multiple active or legacy sources. An authority source needs covered dates and an explicit capability record.

The collector must support server-side sessions. IDOX and Camden's JSF search both use per-session request state.

The collector must support JavaScript capture. Haringey and Dorset did not expose application data in the first HTML response.

The collector must record portal blocks as coverage gaps. A maintenance page, HTTP 503, or blank client bootstrap is not an empty application result.

The collector must keep source-specific comment semantics. West Suffolk publishes representations in the documents section. Leeds states that public comment text is not published. The common schema must report those differences rather than infer equivalent completeness.

## Verification status

Arun, Barnet, Camden, Cornwall, Devon, Durham, Haringey, OPDC, Peak District,
and West Suffolk have recorded detail paths. OPDC, Peak District, and West
Suffolk have completed receipt-backed persisted live bootstraps and are
live-ready for their verified HTTP contracts. Camden and Devon have also
completed verified bootstraps, with their later weekly cycles still pending.
Cheshire East and Leeds have recorded discovery paths but inconclusive detail
retrieval. Dorset has a verified map-client boundary without bounded
application discovery. Blackburn and Birmingham remain blocked at their entry
points. Every authority has a dated walkthrough, while unresolved paths remain
explicit.
