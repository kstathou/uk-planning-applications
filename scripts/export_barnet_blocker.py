# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Print a sanitized blocker artifact from a retained Barnet qualification."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from yimby.authorities.barnet.blocker import (
    BarnetBlockerEvidenceError,
    derive_barnet_blocker,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--confirm-official-http-429", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Validate private state and print only its sanitized public aggregate."""
    arguments = _parser().parse_args(argv)
    try:
        artifact = derive_barnet_blocker(
            Path(arguments.data_dir),
            official_http_429_confirmed=arguments.confirm_official_http_429,
        )
    except (
        BarnetBlockerEvidenceError,
        OSError,
        sqlite3.Error,
        ValueError,
    ):
        print(json.dumps({"error": "blocker-evidence-invalid"}), file=sys.stderr)
        return 1
    print(artifact.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
