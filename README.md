# yimby

Tools for working with UK planning applications.

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
