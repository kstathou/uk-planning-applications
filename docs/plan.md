# England planning applications: 15-portal pilot and national rollout

## 1. Summary and agreed defaults

Build `yimby` into a local Python collection system, starting with **15 authority portals** selected for platform, geographical, and authority-type coverage. Each authority owns its scraper, source schema, normalisation rules, and tests.

| Area | Decision |
|---|---|
| National scope | All English local planning authorities, including counties, national parks, and development corporations |
| Initial collection | Last 30 days plus older open applications |
| Refresh | Weekly for active cases and active appeals |
| Decided cases | Weekly for 90 days after decision, then quarterly |
| Attachments | Retain metadata and source links; never fetch attachment bodies |
| Comments | Retain exposed text; explicitly flag PDF-only text as unavailable |
| History | Preserve observed changes from collection onward |
| Storage | Local SQLite database and compressed source evidence |
| Operation | Manual commands initially; scheduling examples supplied |
| Publication | Local research first, separate public exports later |

Include all application types within planning registers and their associated appeals, consultations, conditions, and relationships. Separate building-control and enforcement registers, national infrastructure casework, and historical backfilling remain outside the initial scope.

## 2. Pilot selection and browser investigation

### The 15-authority pilot

Implement in three waves of five. All 15 belong to the pilot; national rollout begins after their validation.

| Wave | Authority | Main coverage contribution |
|---|---|---|
| 1 | Barnet | Idox search and weekly-list workflows |
| 1 | Camden | Multi-page records and separate document service |
| 1 | Haringey | JavaScript-loaded records, hidden fields, paginated comments |
| 1 | Devon County Council | Minerals, waste, and county development |
| 1 | Peak District National Park Authority | National-park planning and portal migration behaviour |
| 2 | Arun | Ocella and embedded search interface |
| 2 | Old Oak and Park Royal Development Corporation | Development-corporation cases and delegated applications |
| 2 | Dorset | Consolidated authority and register integration |
| 2 | Cheshire East | Alternative search workflow and reference handling |
| 2 | Blackburn with Darwen | Another Planning Explorer implementation |
| 3 | Birmingham | Metropolitan authority and Planning Explorer variation |
| 3 | Leeds | Large urban authority and geographical searches |
| 3 | Cornwall | Geographically large unitary authority |
| 3 | Durham County Council | North East and unitary-county coverage |
| 3 | West Suffolk | East of England, ward/parish filters, weekly lists |

The additions include confirmed distinct implementations: [Arun's Ocella interface](https://www.arun.gov.uk/planning-application-search/), [OPDC's Agile/APAS system](https://www.london.gov.uk/adhs13-apas-back-office-planning-system-fee-2025-26), and [Blackburn's Planning Explorer](https://planning.blackburn.gov.uk/Northgate/PlanningExplorer/ApplicationSearch.aspx). Confirm each authority's current portal and supporting services during its walkthrough.

### Required investigation for every authority

Before writing its scraper:

1. Navigate recent, open, decided, and comment/document-rich applications.
2. Record search filters, date meanings, pagination, result caps, identifiers, tabs, expandable controls, and document services.
3. Inspect the public requests and JavaScript data that populate those pages.
4. Document retrieval of every exposed section, including how to distinguish empty results from loading failures.
5. Save a walkthrough, field inventory, sanitised fixtures, and limitations alongside the implementation.

Build a national authority registry from the [official planning-authority dataset](https://www.planning.data.gov.uk/dataset/local-planning-authority), reconciled against official authority websites. Support multiple portals per authority, shared portals, and predecessor/successor relationships. Record covered periods, implementation status, capabilities, and collection freshness.

## 3. Architecture, interfaces, and local tools

### Independent scrapers

Each authority gets its own package containing extraction logic, versioned Pydantic models, normalisation, fixtures, and tests. Keep council-specific logic local even when copied from another implementation.

Share operational infrastructure: HTTP/browser sessions, throttling, retries, orchestration, persistence, and logging.

Use `httpx` and HTML parsing where possible. Use Python Playwright for JavaScript-dependent workflows and relevant public page objects. Routine collection requires no LLM calls.

Common adapter interface:

- `discover(window, checkpoint)` → references and resumable progress.
- `fetch(reference)` → native payload with section completeness.
- `normalise(payload)` → common records with provenance.

### SQLite and source evidence

Use `sqlite3`, migrations, foreign keys, WAL mode, and one coordinated writer. Store:

- Authorities, runs, checkpoints, retry queues, and refresh schedules.
- Council-native JSON versions, preserving unmapped fields.
- Normalised applications, documents, comments, events, relationships, and locations.
- Observation timestamps, semantic hashes, completeness, and normaliser versions.

Keep compressed HTML/JSON evidence for changed records in a content-addressed directory. Retain failure diagnostics for 30 days and exclude session secrets. Export JSONL, CSV, and Parquet from SQLite.

Assign stable internal application IDs with mappings to portal identifiers and reference aliases. Never merge records solely because their addresses match.

The common schema covers references, proposal, type, status, decision, dates, address, geographical identifiers, published parties, officers, constraints, conditions, consultations, relationships, document metadata, and comments. Preserve native values alongside normalised categories.

Distinguish empty, unavailable, excluded-by-policy, and failed sections. Convert supplied British National Grid coordinates to WGS84; leave missing locations unknown.

### Commands and dashboard

```text
yimby authorities
yimby bootstrap --authority <id|all> --days 30 --include-open
yimby sync --authority <id|all>
yimby inspect <application-id>
yimby normalise --rebuild
yimby export --format <jsonl|csv|parquet> --profile <research|public>
yimby dashboard
yimby backup
yimby restore <backup>
yimby doctor
```

Build a local Streamlit dashboard showing authority coverage, freshness, failures, backlog, collection costs, application search, observed changes, and a map. Display coverage denominators and unmapped-record counts alongside metrics.

Provide consistent backups, restore verification, disk-space checks, and launchd/systemd examples without enabling unattended execution.

Public exports use a reviewed field allowlist and source reuse information. Initially exclude personal-party fields, comment bodies, raw payloads, and unrestricted history; include aggregate comment counts and source links. Support suppression corrections.

## 4. Incremental collection and reliability

### Discovery and refresh

- Bootstrap 30 days of applications and enumerate older open cases.
- Query supported received, validated, decision, and change-date searches from the last successful checkpoint with a 30-day overlap.
- Re-enumerate open cases weekly and refresh known records according to the agreed policy.
- Reconcile the preceding 90 days quarterly.
- Consume reliable change feeds where available, including references outside the initial window.
- Split capped searches into smaller date ranges, exhaust pagination, and reconcile reported counts.

Advance discovery checkpoints only after references are durably queued. Failed detail requests remain retryable.

### Version history

Hash semantic application sections, documents, and comments while excluding transport timestamps, tokens, and irrelevant ordering. Unchanged observations update freshness without creating duplicate versions.

Preserve successive changes and reversions. Distinguish source changes from normalisation changes.

Failed child requests must never overwrite existing sections with empty data. Reconcile disappearance only after complete successful enumeration, retaining earlier observations and availability flags.

History records observed states. Attachment replacements cannot be detected when their exposed metadata stays unchanged, and changes between collection runs may be unobserved.

### Runtime defaults

Allow four concurrent authorities, one browser worker, and one request at a time per host with a minimum two-second gap. Honour longer portal limits and `Retry-After`, use bounded retries, and isolate failing authorities.

Prevent overlapping collectors and resume interrupted work. Report request counts, browser time, transferred bytes, duration, and storage growth. Flag blocked portals explicitly.

## 5. Delivery and acceptance criteria

### Delivery sequence

1. Build the registry, adapter interface, storage, resumable runner, and CLI.
2. Investigate all 15 portals and record their capabilities before finalising the first common schema.
3. Implement the three pilot waves, adding normalisation and fixtures with each scraper.
4. Complete exports, dashboard, backup/restore, and operating documentation.
5. Validate all 15 through two weekly collection cycles and report completeness, runtime, and growth.
6. Expand nationally in batches of ten authorities, keeping each implementation independent and unresolved coverage gaps visible.

### Required tests

- Browser-to-scraper agreement for exposed fields and complete paginated document/comment lists.
- Discovery of late-published applications, older open cases, and older cases appearing in decision searches.
- Detection of new comments, changed decisions, document-metadata changes, removals, and reversions.
- No duplicate applications or semantic versions on unchanged reruns.
- Recovery from interrupted runs, expired sessions, rate limits, capped searches, and partial failures.
- Verification that attachment bodies are never fetched, including through browser previews.
- Rebuilding normalised data from retained payloads without contacting authorities.
- Public-export exclusions, coordinate conversion, backup restoration, and dashboard completeness indicators.

Use sanitised fixtures and simulated JavaScript pages in deterministic CI, with live portal smoke checks separately. Preserve the existing Ruff, strict typing, build, and 100% branch-coverage gates.

The pilot passes only when all 15 have verified discovery, extraction, incremental refresh, and explicit completeness reporting. National coverage remains a separate completion milestone.
