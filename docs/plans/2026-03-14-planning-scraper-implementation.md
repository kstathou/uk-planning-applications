# Planning Scraper Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Set up the repo and build scrapers for Haringey and Hackney planning portals, outputting JSONL files for schema exploration.

**Architecture:** browser-use agents (one per council) driven by a vLLM endpoint on Modal. Each agent navigates the council's planning portal, extracts application data via a custom `save_application` tool, and writes JSONL to a Modal Volume. Scrapers run as Modal serverless functions.

**Tech Stack:** Python 3.13, uv, browser-use, Playwright/Chromium, Modal

---

### Task 1: Repo setup with uv

**Files:**
- Create: `pyproject.toml`
- Modify: `.gitignore`

**Step 1: Initialise uv project**

Run:
```bash
cd /Users/kostasstathoulopoulos/Desktop/personal/uk-planning-applications
uv init --no-readme
```

**Step 2: Replace pyproject.toml with our config**

```toml
[project]
name = "uk-planning-applications"
version = "0.1.0"
description = "Centralised planning application data for England"
requires-python = ">=3.13"
dependencies = [
    "browser-use",
    "modal",
    "python-dotenv",
]

[dependency-groups]
dev = [
    "pytest",
    "pytest-asyncio",
]
```

**Step 3: Add to .gitignore**

Append:
```
# Project data
data/

# Environment
.env
```

**Step 4: Create directory structure**

Run:
```bash
mkdir -p backend/scrapers backend/prompts backend/tools backend/modal_jobs data
touch backend/__init__.py backend/scrapers/__init__.py backend/prompts/__init__.py backend/tools/__init__.py backend/modal_jobs/__init__.py
```

**Step 5: Install dependencies and Chromium**

Run:
```bash
uv sync
uvx browser-use install
```

**Step 6: Create .env file**

```bash
# .env
VLLM_ENDPOINT=  # user fills in their Modal vLLM endpoint
VLLM_API_KEY=   # user fills in their API key
VLLM_MODEL=     # user fills in the model name
ANONYMIZED_TELEMETRY=false
```

**Step 7: Commit**

```bash
git add pyproject.toml uv.lock .gitignore backend/ .env.example
git commit -m "feat: initialise project with uv, browser-use, modal"
```

Note: copy `.env` to `.env.example` with empty values before committing. Never commit `.env`.

---

### Task 2: save_application custom tool

**Files:**
- Create: `backend/tools/save.py`
- Create: `tests/test_save_tool.py`

**Step 1: Write the failing test**

```python
# tests/test_save_tool.py
import json
import os
import tempfile
import pytest
from backend.tools.save import create_save_tools


def test_save_application_writes_jsonl():
    with tempfile.TemporaryDirectory() as tmpdir:
        tools = create_save_tools(output_dir=tmpdir)

        # Find the save_application action function
        save_fn = None
        for action in tools.registry.actions.values():
            if action.name == "save_application":
                save_fn = action.function
                break
        assert save_fn is not None, "save_application tool not registered"

        result = save_fn(
            reference="HGY/2026/0001",
            address="123 High Road, London N22",
            description="Single storey rear extension",
            status="pending",
            submitted_date="2026-03-01",
            decision_date="",
            applicant="John Smith",
            council="haringey",
            url="https://planningservices.haringey.gov.uk/portal/123",
            raw_fields={"ward": "Hornsey", "officer": "Jane Doe"},
        )

        assert "Saved" in result

        filepath = os.path.join(tmpdir, "haringey.jsonl")
        assert os.path.exists(filepath)

        with open(filepath) as f:
            line = f.readline()
            data = json.loads(line)

        assert data["reference"] == "HGY/2026/0001"
        assert data["council"] == "haringey"
        assert data["raw_fields"]["ward"] == "Hornsey"
        assert data["url"] == "https://planningservices.haringey.gov.uk/portal/123"


def test_save_application_appends_multiple():
    with tempfile.TemporaryDirectory() as tmpdir:
        tools = create_save_tools(output_dir=tmpdir)
        save_fn = None
        for action in tools.registry.actions.values():
            if action.name == "save_application":
                save_fn = action.function
                break

        for i in range(3):
            save_fn(
                reference=f"REF/{i}",
                address=f"Address {i}",
                description="Desc",
                status="pending",
                submitted_date="2026-01-01",
                decision_date="",
                applicant="Applicant",
                council="test_council",
                url=f"https://example.com/{i}",
                raw_fields={},
            )

        filepath = os.path.join(tmpdir, "test_council.jsonl")
        with open(filepath) as f:
            lines = f.readlines()
        assert len(lines) == 3
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_save_tool.py -v`
Expected: FAIL (module not found)

**Step 3: Write the implementation**

```python
# backend/tools/save.py
import json
import os
from datetime import datetime, timezone

from browser_use import Tools


def create_save_tools(output_dir: str = "data") -> Tools:
    """Create a Tools instance with the save_application action."""
    tools = Tools()

    @tools.action(description=(
        "Save a planning application to the output file. "
        "Call this for EACH application you find on the portal. "
        "Put any fields that don't fit the named parameters into raw_fields."
    ))
    def save_application(
        reference: str,
        address: str,
        description: str,
        status: str,
        submitted_date: str,
        decision_date: str,
        applicant: str,
        council: str,
        url: str,
        raw_fields: dict,
    ) -> str:
        record = {
            "reference": reference,
            "address": address,
            "description": description,
            "status": status,
            "submitted_date": submitted_date,
            "decision_date": decision_date,
            "applicant": applicant,
            "council": council,
            "url": url,
            "raw_fields": raw_fields,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, f"{council}.jsonl")
        with open(filepath, "a") as f:
            f.write(json.dumps(record) + "\n")
        return f"Saved application {reference}"

    return tools
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_save_tool.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/tools/save.py tests/test_save_tool.py
git commit -m "feat: add save_application custom tool for browser-use"
```

---

### Task 3: Base scraper setup

**Files:**
- Create: `backend/scrapers/base.py`
- Create: `tests/test_base_scraper.py`

**Step 1: Write the failing test**

```python
# tests/test_base_scraper.py
import pytest
from unittest.mock import patch
from backend.scrapers.base import create_agent


def test_create_agent_returns_agent():
    agent = create_agent(
        task="Test task",
        vllm_endpoint="http://localhost:8000/v1",
        vllm_api_key="test-key",
        vllm_model="test-model",
        output_dir="/tmp/test_data",
        headless=True,
    )
    # Agent should be created without errors
    assert agent is not None
    assert agent.task == "Test task"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_base_scraper.py -v`
Expected: FAIL (module not found)

**Step 3: Write the implementation**

```python
# backend/scrapers/base.py
from browser_use import Agent, Browser, ChatOpenAI

from backend.tools.save import create_save_tools


def create_agent(
    task: str,
    vllm_endpoint: str,
    vllm_api_key: str,
    vllm_model: str,
    output_dir: str = "data",
    headless: bool = True,
) -> Agent:
    """Create a browser-use agent configured for planning portal scraping."""
    llm = ChatOpenAI(
        model=vllm_model,
        base_url=vllm_endpoint,
        api_key=vllm_api_key,
    )

    browser = Browser(headless=headless)

    tools = create_save_tools(output_dir=output_dir)

    agent = Agent(
        task=task,
        llm=llm,
        browser=browser,
        tools=tools,
        use_vision=True,
        max_actions_per_step=3,
        generate_gif=False,
    )

    return agent
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_base_scraper.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/scrapers/base.py tests/test_base_scraper.py
git commit -m "feat: add base scraper with browser-use agent factory"
```

---

### Task 4: Haringey prompt and scraper

**Files:**
- Create: `backend/prompts/haringey.py`
- Create: `backend/scrapers/haringey.py`

**Step 1: Write the Haringey prompt**

```python
# backend/prompts/haringey.py
PORTAL_URL = "http://www.planningservices.haringey.gov.uk/portal/"

TASK = f"""
Go to {PORTAL_URL}

Your goal is to find and save recent planning applications from Haringey Council.

1. Look for a search page or weekly list of applications.
2. If there is a search form, search for applications from the last 30 days.
   If there is a weekly list or recent applications page, use that instead.
3. For each application found, click into its detail page.
4. Extract all available fields: reference number, address, description of proposed
   work, application status, key dates (submitted, validated, decided), applicant name,
   and the URL of the detail page.
5. Call save_application for EACH application. Put the core fields in the named
   parameters. Put ALL other fields you find on the page into raw_fields as a dict.
6. After saving, go back to the list and continue with the next application.
7. Process at least 20 applications, or all available if fewer than 20.

Important:
- If a field is not available, pass an empty string "".
- Always set council to "haringey".
- If you encounter a CAPTCHA or error page, try refreshing or navigating back.
- If the page uses frames or iframes, look inside them for content.
"""
```

**Step 2: Write the Haringey scraper**

```python
# backend/scrapers/haringey.py
import asyncio

from backend.prompts.haringey import TASK
from backend.scrapers.base import create_agent


async def scrape_haringey(
    vllm_endpoint: str,
    vllm_api_key: str,
    vllm_model: str,
    output_dir: str = "data",
) -> None:
    """Scrape planning applications from Haringey Council."""
    agent = create_agent(
        task=TASK,
        vllm_endpoint=vllm_endpoint,
        vllm_api_key=vllm_api_key,
        vllm_model=vllm_model,
        output_dir=output_dir,
        headless=True,
    )
    history = await agent.run(max_steps=100)
    print(f"Haringey scrape complete. Steps: {history.number_of_steps()}")
    if history.has_errors():
        print(f"Errors: {history.errors()}")


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    load_dotenv()
    asyncio.run(
        scrape_haringey(
            vllm_endpoint=os.environ["VLLM_ENDPOINT"],
            vllm_api_key=os.environ["VLLM_API_KEY"],
            vllm_model=os.environ["VLLM_MODEL"],
            output_dir="data",
        )
    )
```

**Step 3: Commit**

```bash
git add backend/prompts/haringey.py backend/scrapers/haringey.py
git commit -m "feat: add Haringey scraper and prompt"
```

---

### Task 5: Hackney prompt and scraper

**Files:**
- Create: `backend/prompts/hackney.py`
- Create: `backend/scrapers/hackney.py`

**Step 1: Write the Hackney prompt**

```python
# backend/prompts/hackney.py
PORTAL_URL = "http://planning.hackney.gov.uk/Northgate/PlanningExplorer/generalsearch.aspx"

TASK = f"""
Go to {PORTAL_URL}

Your goal is to find and save recent planning applications from Hackney Council.
This is a Northgate PlanningExplorer portal.

1. You should see a search form. Look for date fields — search for applications
   validated or received in the last 30 days.
2. If the form has a "Date Type" dropdown, select "Received" or "Validated".
   Set the date range to the last 30 days.
3. Click the Search button.
4. You should see a results list. For each application in the list, click into
   its detail page.
5. Extract all available fields: reference number, address, description of proposed
   work, application type, application status, key dates (received, validated, decided,
   target date), applicant name, agent name, ward, case officer, and the URL of the
   detail page.
6. Call save_application for EACH application. Put the core fields in the named
   parameters. Put ALL other fields (application type, ward, case officer, agent,
   target date, etc.) into raw_fields as a dict.
7. After saving, go back to the results list and continue with the next application.
8. If there are multiple pages of results, navigate through them.
9. Process at least 20 applications, or all available if fewer than 20.

Important:
- If a field is not available, pass an empty string "".
- Always set council to "hackney".
- Northgate forms often use ASP.NET postbacks — wait for page loads after clicking.
- If you encounter an error, try go_back and retry the search.
"""
```

**Step 2: Write the Hackney scraper**

```python
# backend/scrapers/hackney.py
import asyncio

from backend.prompts.hackney import TASK
from backend.scrapers.base import create_agent


async def scrape_hackney(
    vllm_endpoint: str,
    vllm_api_key: str,
    vllm_model: str,
    output_dir: str = "data",
) -> None:
    """Scrape planning applications from Hackney Council."""
    agent = create_agent(
        task=TASK,
        vllm_endpoint=vllm_endpoint,
        vllm_api_key=vllm_api_key,
        vllm_model=vllm_model,
        output_dir=output_dir,
        headless=True,
    )
    history = await agent.run(max_steps=100)
    print(f"Hackney scrape complete. Steps: {history.number_of_steps()}")
    if history.has_errors():
        print(f"Errors: {history.errors()}")


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    load_dotenv()
    asyncio.run(
        scrape_hackney(
            vllm_endpoint=os.environ["VLLM_ENDPOINT"],
            vllm_api_key=os.environ["VLLM_API_KEY"],
            vllm_model=os.environ["VLLM_MODEL"],
            output_dir="data",
        )
    )
```

**Step 3: Commit**

```bash
git add backend/prompts/hackney.py backend/scrapers/hackney.py
git commit -m "feat: add Hackney scraper and prompt (Northgate portal)"
```

---

### Task 6: Modal deployment

**Files:**
- Create: `backend/modal_jobs/scrape.py`

**Step 1: Write the Modal function**

```python
# backend/modal_jobs/scrape.py
import modal

app = modal.App("planning-scrapers")

volume = modal.Volume.from_name("planning-data", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.13")
    .pip_install("browser-use", "python-dotenv")
    .run_commands(
        "uvx browser-use install",
    )
)

SCRAPERS = {
    "haringey": "backend.scrapers.haringey:scrape_haringey",
    "hackney": "backend.scrapers.hackney:scrape_hackney",
}


@app.function(
    image=image,
    volumes={"/data": volume},
    timeout=600,
    secrets=[modal.Secret.from_name("vllm-config")],
)
async def scrape_council(council: str):
    import importlib
    import os

    module_path, func_name = SCRAPERS[council].rsplit(":", 1)
    module = importlib.import_module(module_path)
    scrape_fn = getattr(module, func_name)

    await scrape_fn(
        vllm_endpoint=os.environ["VLLM_ENDPOINT"],
        vllm_api_key=os.environ["VLLM_API_KEY"],
        vllm_model=os.environ["VLLM_MODEL"],
        output_dir="/data",
    )

    volume.commit()
    print(f"Committed data for {council} to volume")


@app.local_entrypoint()
async def main(council: str = "haringey"):
    await scrape_council.remote.aio(council)
```

**Step 2: Create the Modal secret**

Run (one-time setup):
```bash
modal secret create vllm-config \
    VLLM_ENDPOINT=<your-endpoint> \
    VLLM_API_KEY=<your-key> \
    VLLM_MODEL=<your-model>
```

**Step 3: Test locally first (optional)**

Before deploying to Modal, test scrapers locally:
```bash
uv run python -m backend.scrapers.haringey
```
This runs locally using your `.env` and writes to `data/haringey.jsonl`.

**Step 4: Deploy and run on Modal**

```bash
modal run backend/modal_jobs/scrape.py --council haringey
modal run backend/modal_jobs/scrape.py --council hackney
```

**Step 5: Download results from Modal Volume**

```bash
modal volume get planning-data haringey.jsonl data/haringey.jsonl
modal volume get planning-data hackney.jsonl data/hackney.jsonl
```

**Step 6: Commit**

```bash
git add backend/modal_jobs/scrape.py
git commit -m "feat: add Modal deployment for council scrapers"
```

---

### Task 7: Inspect and compare JSONL output

This is a manual exploration step. After running both scrapers:

**Step 1: Check the output files**

```bash
wc -l data/*.jsonl
head -1 data/haringey.jsonl | python -m json.tool
head -1 data/hackney.jsonl | python -m json.tool
```

**Step 2: Compare raw_fields across councils**

```python
# Quick script to see what raw_fields each council provides
import json

for council in ["haringey", "hackney"]:
    print(f"\n=== {council} ===")
    keys = set()
    with open(f"data/{council}.jsonl") as f:
        for line in f:
            record = json.loads(line)
            keys.update(record["raw_fields"].keys())
    print(f"Raw fields: {sorted(keys)}")
```

**Step 3: Decide on schema**

Based on the output, decide which `raw_fields` should be promoted to top-level fields in the future database schema. This informs the Neon Postgres table design.
