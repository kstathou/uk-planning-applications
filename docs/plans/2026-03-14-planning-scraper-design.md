# UK Planning Applications — Scraper Design

## Goal

Centralise planning application data from all local authorities in England into a single dataset. Start by scraping two councils (Haringey and Hackney) to understand data shapes, then generalise.

## Architecture

### Scraping

- **browser-use** (open-source) controls a headless Chromium browser via Playwright
- An LLM (served via vLLM on Modal, OpenAI-compatible endpoint) drives the browser-use agent
- Each council gets a tailored task prompt that encodes knowledge of that portal's layout and navigation
- A custom `save_application` tool lets the agent write each application incrementally to JSONL
- The tool includes a `raw_fields: dict` catch-all so we can discover what fields each portal provides before committing to a schema

### Compute

- **Modal** runs each scrape as a serverless function — no idle costs
- One parameterised Modal function handles all councils
- Timeout: ~10 minutes per council (adjustable)
- No scheduling during exploration — manual triggers only
- JSONL output written to a **Modal Volume** (persistent shared disk)

### Council portal landscape

England has ~300+ local authorities. Most use one of a handful of portal platforms:

| Platform   | Example councils          | URL pattern                          |
|------------|---------------------------|--------------------------------------|
| Idox       | Camden, Bristol, Leeds    | `/online-applications/`              |
| Northgate  | Hackney, Birmingham       | `/Northgate/PlanningExplorer/`       |
| Agile      | Various                   | Varies                               |
| Ocella     | Various                   | Varies                               |
| Bespoke    | Haringey                  | Custom servlet-based                 |

This means ~5-8 scraper templates will eventually cover 80-90% of councils. Bespoke portals are where browser-use's flexibility is most valuable.

### Starting councils

- **Haringey** — bespoke Java servlet portal (`planningservices.haringey.gov.uk/portal/`). Good test of browser-use on non-standard sites.
- **Hackney** — Northgate (`planning.hackney.gov.uk/Northgate/PlanningExplorer/`). Tests one of the major platforms.

## Project structure

```
uk-planning-applications/
├── backend/
│   ├── scrapers/
│   │   ├── base.py           # shared browser/agent setup
│   │   ├── haringey.py       # council-specific scraper config
│   │   └── hackney.py
│   ├── prompts/
│   │   ├── haringey.py       # task prompt + portal URL
│   │   └── hackney.py
│   ├── tools/
│   │   └── save.py           # save_application custom tool
│   └── modal_jobs/
│       └── scrape.py         # Modal function entrypoint
├── data/                     # gitignored, local JSONL output
├── frontend/                 # Next.js + Mapbox (later)
├── pyproject.toml            # uv-managed Python project
└── .gitignore
```

## Data flow

```
Modal triggers scrape_council("haringey", vllm_endpoint)
  → browser-use Agent loads prompt from prompts/haringey.py
  → Agent opens headless Chromium, navigates portal
  → For each application found, agent calls save_application tool
  → Tool appends JSON line to Modal Volume: data/haringey.jsonl
```

## save_application tool schema (exploration phase)

```python
def save_application(
    reference: str,        # application reference number
    address: str,          # site address
    description: str,      # proposed work
    status: str,           # pending / approved / refused / withdrawn
    submitted_date: str,   # date submitted
    decision_date: str,    # date of decision (if any)
    applicant: str,        # applicant name
    council: str,          # council name
    raw_fields: dict,      # catch-all for extra fields
) -> str:
```

The `raw_fields` dict captures everything else the agent finds. After scraping both councils we will compare the JSONL output to design a harmonised schema.

## Database (later)

- **Neon Postgres** with **PostGIS** extension for geospatial queries
- Scale-to-zero: no cost when idle
- Free tier: 500MB storage, 100 CU-hours/month
- Native Vercel integration (Vercel retired its own Postgres in favour of Neon)
- Schema to be designed after analysing JSONL from initial scrapes

## Frontend (later)

- **Next.js** deployed on **Vercel**
- **Mapbox GL JS** for interactive map of applications
- Charting library (TBD) for analytics dashboards
- Filters by council, status, date range, application type, geographic area

## What to build first

1. Repo setup — uv, pyproject.toml, project structure
2. `save_application` custom tool
3. Haringey scraper + prompt
4. Hackney scraper + prompt
5. Modal deployment config (image with browser-use + Playwright + Chromium)
6. Run both scrapers, inspect JSONL output
7. Compare fields across councils, design harmonised schema
