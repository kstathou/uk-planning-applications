# Copyright (c) 2026 Kostas Stathoulopoulos

"""Consistent, verified SQLite and evidence backups."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from yimby.store import SqliteStore


class BackupTargetExistsError(FileExistsError):
    """A backup would overwrite an existing target."""


class RestoreTargetExistsError(FileExistsError):
    """A restore would overwrite an existing data directory."""


class BackupVerificationError(RuntimeError):
    """A backup manifest, database, or evidence file failed verification."""

    def __init__(self, reason: str) -> None:
        """Name the failed verification boundary."""
        super().__init__(f"backup verification failed: {reason}")


class _VerificationFailure(StrEnum):
    DATABASE = "database integrity"
    MANIFEST = "missing manifest"
    RESTORED_EVIDENCE = "restored evidence"
    SCHEMA = "unsupported manifest schema"


def create_backup(store: SqliteStore, destination: Path) -> Path:
    """Snapshot SQLite, copy evidence, and write content hashes."""
    if destination.exists():
        raise BackupTargetExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    database = destination / "yimby.sqlite3"
    store.backup_database(database)
    evidence = destination / "evidence"
    if store.evidence_root.exists():
        shutil.copytree(store.evidence_root, evidence)
    else:
        evidence.mkdir()
    files = _payload_files(destination)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "files": {
            str(path.relative_to(destination)): {
                "sha256": _digest(path),
                "size": path.stat().st_size,
            }
            for path in files
        },
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    )
    verify_backup(destination)
    return destination


def verify_backup(backup: Path) -> None:
    """Verify every manifest hash and the SQLite snapshot integrity."""
    manifest_path = backup / "manifest.json"
    if not manifest_path.is_file():
        raise BackupVerificationError(_VerificationFailure.MANIFEST)
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1:
        raise BackupVerificationError(_VerificationFailure.SCHEMA)
    for relative, expected in manifest["files"].items():
        path = backup / relative
        if (
            not path.is_file()
            or path.stat().st_size != expected["size"]
            or _digest(path) != expected["sha256"]
        ):
            raise BackupVerificationError(relative)
    database = backup / "yimby.sqlite3"
    try:
        with closing(sqlite3.connect(database)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as error:
        raise BackupVerificationError(_VerificationFailure.DATABASE) from error
    if integrity != ("ok",):
        raise BackupVerificationError(_VerificationFailure.DATABASE)


def restore_backup(backup: Path, target: Path) -> Path:
    """Restore a verified backup into a new data directory only."""
    verify_backup(backup)
    if target.exists():
        raise RestoreTargetExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    shutil.copy2(backup / "yimby.sqlite3", target / "yimby.sqlite3")
    shutil.copytree(backup / "evidence", target / "evidence")
    _verify_restored_evidence(target)
    return target


def default_backup_path(data_dir: Path) -> Path:
    """Return a collision-resistant backup directory below local data."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return data_dir / "backups" / stamp


def _verify_restored_evidence(target: Path) -> None:
    database = target / "yimby.sqlite3"
    evidence_root = target / "evidence"
    with closing(sqlite3.connect(database)) as connection:
        paths = tuple(row[0] for row in connection.execute("SELECT path FROM evidence"))
    missing = tuple(path for path in paths if not (evidence_root / path).is_file())
    if missing:
        raise BackupVerificationError(_VerificationFailure.RESTORED_EVIDENCE)


def _payload_files(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            (path for path in root.rglob("*") if path.is_file()),
            key=lambda path: str(path.relative_to(root)),
        )
    )


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()
