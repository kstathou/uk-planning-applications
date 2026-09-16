# Copyright (c) 2026 Kostas Stathoulopoulos

"""Actionable local health diagnostics."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from yimby.domain import DoctorCheck, DoctorReport

if TYPE_CHECKING:
    from pathlib import Path

    from yimby.store import SqliteStore

EXPECTED_MIGRATIONS = (1, 2, 3, 4, 5, 8)
DEFAULT_MINIMUM_FREE_BYTES = 100 * 1024 * 1024


def run_doctor(
    store: SqliteStore,
    data_dir: Path,
    *,
    expected_authorities: int,
    minimum_free_bytes: int = DEFAULT_MINIMUM_FREE_BYTES,
) -> DoctorReport:
    """Check database, migrations, evidence, registry, and free disk space."""
    integrity = store.database_integrity()
    migrations = store.migration_versions()
    invalid_evidence = store.invalid_evidence_paths()
    authority_count = len(store.authority_states())
    free_bytes = shutil.disk_usage(data_dir).free
    return DoctorReport(
        checks=(
            DoctorCheck(
                name="sqlite-integrity",
                ok=integrity == "ok",
                detail=integrity,
            ),
            DoctorCheck(
                name="migrations",
                ok=migrations == EXPECTED_MIGRATIONS,
                detail=",".join(str(version) for version in migrations),
            ),
            DoctorCheck(
                name="evidence",
                ok=not invalid_evidence,
                detail=(
                    "complete"
                    if not invalid_evidence
                    else f"invalid {len(invalid_evidence)} file(s)"
                ),
            ),
            DoctorCheck(
                name="authority-registry",
                ok=authority_count == expected_authorities,
                detail=f"{authority_count}/{expected_authorities}",
            ),
            DoctorCheck(
                name="disk-space",
                ok=free_bytes >= minimum_free_bytes,
                detail=f"{free_bytes} bytes free",
            ),
        )
    )
