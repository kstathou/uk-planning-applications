# yimby

Local collection, normalisation, evidence, export, and operations tooling for
the 15-authority England planning-register pilot.

All 15 authority packages have typed native schemas and deterministic fixtures.
Live portal readiness is tracked separately and remains partial while real
adapters and the required two weekly validation cycles are completed. See the
[pilot acceptance ledger](docs/pilot-acceptance.md) for the current boundary.
No authority is described as live-collection verified until a complete real
bootstrap has been persisted and compared with its dated walkthrough.

## Requirements

- [uv](https://docs.astral.sh/uv/) 0.12.15 or newer
- Python 3.13 (installed automatically by uv when needed)

## Set up a development environment

```sh
uv sync --locked
cp .env.example .env
uv run pre-commit install --install-hooks
```

The `dev` dependency group is installed by default. The committed `uv.lock`
keeps local and CI environments reproducible.

## Use the pilot

```sh
# List the fixed 15-authority registry and live-readiness evidence.
uv run yimby authorities

# Exercise all packages without network access.
uv run yimby bootstrap --authority all --days 30 --include-open --fixture

# Inspect the local operational model without launching a server.
uv run yimby dashboard --json

# Check the database, evidence set, migrations, registry, and disk space.
uv run yimby doctor
```

Run `uv run yimby --help` for the complete command surface. The
[operating guide](docs/operations.md) covers live-versus-fixture behavior,
exports, the Streamlit dashboard, backups, restore, and disabled scheduling
examples. The [architecture](docs/architecture.md) explains the authority-owned
adapter and single-writer storage boundaries.

## Development commands

```sh
# Run the complete local quality gate.
uv run pre-commit run --all-files
uv run pre-commit run --hook-stage pre-push --all-files

# Run tools individually.
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest

# Build the wheel and source distribution.
uv build --no-sources
```

Ruff supplies formatting, import sorting (isort rules), Bandit-compatible
security checks, and the rest of its stable lint rule set. Pytest enforces
100% branch coverage for the scaffold.

## Environment variables

Copy `.env.example` to `.env` for local values. The real `.env` is ignored by
Git; only safe example values belong in `.env.example`.
