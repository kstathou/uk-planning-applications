# Pilot portal inventory

This reference records the browser census begun on 15 September 2026 and the
dated Dorset follow-up on 16 September 2026. A successful entry-page check does
not prove extraction completeness. Each authority still needs detail-page,
pagination, failure, and incremental-refresh checks before live verification
can pass.

| Authority | Observed sources | Portal shape | Browser result |
|---|---|---|---|
| Barnet | `publicaccess.barnet.gov.uk/online-applications/` | IDOX Public Access | Weekly discovery and one detail record verified. See `authorities/barnet.md`. |
| Camden | Current JSF search, Northgate record pages, and CMWebDrawer documents | Three linked services | An exact-reference search and one decided record were verified. See `authorities/camden.md`. |
| Haringey | `londonboroughofharingey.my.site.com/pr/s/` | Salesforce public register | Seven-day discovery, one detail record, one explicit empty comment section, and its six-row file index were verified. See `authorities/haringey.md`. |
| Devon County Council | `planning.devon.gov.uk/` | Custom register | A 90-day discovery query, explicit zero-result query, and one detail record were verified. See `authorities/devon.md`. |
| Peak District National Park Authority | Legacy custom portal plus AssureLive replacement | Two linked services | A 33-record weekly query and one detail record were verified. JavaScript-loaded detail sections remained pending. See `authorities/peak-district.md`. |
| Arun | `www1.arun.gov.uk/aplanning/OcellaWeb/` | Ocella | One decided detail record and its document index were verified without opening attachment bodies. See `authorities/arun.md`. |
| Old Oak and Park Royal Development Corporation | `planning.agileapplications.co.uk/opdc` | Agile Applications | Two direct loads reached the citizen-portal title but returned a blank accessible document. See `authorities/opdc.md`. |
| Dorset | `planning.dorsetcouncil.gov.uk` plus the Explorer fixture source | ASP.NET and Telerik statutory register | A 30-day received query, unrestricted outstanding query, 1,438 current detail records, exact document metadata, and zero-network terminal rerun were verified without attachment bodies. See `authorities/dorset.md`. |
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

The collector must support JavaScript capture. Haringey requires it for live
discovery. Dorset Explorer remains a JavaScript fixture boundary, while the
statutory register is collected through captured ASP.NET form requests.

The collector must record portal blocks as coverage gaps. A maintenance page, HTTP 503, or blank client bootstrap is not an empty application result.

The collector must keep source-specific comment semantics. West Suffolk publishes representations in the documents section. Leeds states that public comment text is not published. The common schema must report those differences rather than infer equivalent completeness.

## Verification status

Arun, Barnet, Camden, Cornwall, Devon, Dorset, Durham, Haringey, Peak District,
and West Suffolk have recorded detail paths. Dorset also has a persisted
same-day bootstrap for the required received and older-open inventory. Cheshire
East and Leeds have recorded discovery paths but inconclusive detail retrieval.
Blackburn, Birmingham, and OPDC are blocked at their entry points. Every
authority now has a dated walkthrough, while unresolved paths remain explicit.
