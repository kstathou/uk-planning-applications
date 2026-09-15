# Copyright (c) 2026 Kostas Stathoulopoulos

"""SQLite persistence for durable collection transitions."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from importlib.resources import files
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import TypeAdapter

from yimby.domain import (
    ApplicationId,
    AuthorityId,
    CollectedObservation,
    CommentRecord,
    Completeness,
    DiscoveryState,
    DocumentRecord,
    DurableDiscoveryBatch,
    FrozenModel,
    StoredApplication,
    StoredCheckpoint,
)

if TYPE_CHECKING:
    from pathlib import Path

    from yimby.evidence import EvidenceStore

_DOCUMENTS = TypeAdapter(tuple[DocumentRecord, ...])
_COMMENTS = TypeAdapter(tuple[CommentRecord, ...])
_COMPLETENESS = TypeAdapter(Completeness)


class _ApplicationSection(FrozenModel):
    proposal: str
    status: str


class SqliteStore:
    """Own the only SQLite writer connection for a collection runtime."""

    def __init__(self, path: Path, evidence: EvidenceStore) -> None:
        """Open the writer, enable SQLite safety settings, and migrate."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._evidence = evidence
        self._migrate()

    def close(self) -> None:
        """Close the writer connection."""
        self._connection.close()

    def begin_run(self, authority_id: AuthorityId) -> str:
        """Create a collection run."""
        run_id = str(uuid4())
        with self._connection:
            self._connection.execute(
                "INSERT INTO runs(id, authority_id, started_at) VALUES (?, ?, ?)",
                (run_id, authority_id, datetime.now(UTC).isoformat()),
            )
        return run_id

    def commit_discovery(
        self,
        run_id: str,
        authority_id: AuthorityId,
        batch: DurableDiscoveryBatch,
    ) -> None:
        """Queue references and advance their checkpoint atomically."""
        with self._connection:
            for reference in batch.references:
                self._connection.execute(
                    """
                    INSERT INTO discovery_queue(
                        authority_id, source_id, reference, first_run_id, last_run_id
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, reference) DO UPDATE SET
                        last_run_id = excluded.last_run_id
                    """,
                    (
                        authority_id,
                        reference.source_id,
                        reference.reference,
                        run_id,
                        run_id,
                    ),
                )
            self._connection.execute(
                """
                INSERT INTO checkpoints(
                    authority_id, schema_version, payload_json, run_id
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(authority_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    payload_json = excluded.payload_json,
                    run_id = excluded.run_id
                """,
                (
                    authority_id,
                    batch.next_checkpoint.schema_version,
                    batch.next_checkpoint.payload_json,
                    run_id,
                ),
            )

    def commit_observation(
        self,
        run_id: str,
        collected: CollectedObservation,
    ) -> ApplicationId:
        """Persist evidence and semantic section transitions."""
        normalised = collected.normalised
        application_id = ApplicationId(
            str(
                uuid5(
                    NAMESPACE_URL,
                    (
                        f"{normalised.authority_id}/"
                        f"{normalised.reference.source_id}/"
                        f"{normalised.reference.reference}"
                    ),
                )
            )
        )
        evidence_paths = [
            (capture, self._evidence.put(capture)) for capture in collected.evidence
        ]
        application = _ApplicationSection(
            proposal=normalised.proposal,
            status=normalised.status,
        )
        sections = (
            ("application", application.model_dump_json(), "complete"),
            (
                "documents",
                _DOCUMENTS.dump_json(normalised.documents).decode(),
                normalised.completeness.documents.kind,
            ),
            (
                "comments",
                _COMMENTS.dump_json(normalised.comments).decode(),
                normalised.completeness.comments.kind,
            ),
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO applications(
                    id, authority_id, source_id, reference
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(source_id, reference) DO NOTHING
                """,
                (
                    application_id,
                    normalised.authority_id,
                    normalised.reference.source_id,
                    normalised.reference.reference,
                ),
            )
            native_hash = sha256(collected.native_json.encode()).hexdigest()
            self._connection.execute(
                """
                INSERT OR IGNORE INTO native_versions(
                    application_id, payload_hash, schema_name, payload_json, observed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    native_hash,
                    collected.native_schema,
                    collected.native_json,
                    collected.observed_at.isoformat(),
                ),
            )
            for section, payload, state in sections:
                if state in {"complete", "empty"}:
                    self._commit_section(
                        application_id,
                        section,
                        payload,
                        normalised.normaliser_version,
                    )
            self._connection.execute(
                """
                INSERT INTO observations(
                    run_id, application_id, observed_at, completeness_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    run_id,
                    application_id,
                    collected.observed_at.isoformat(),
                    normalised.completeness.model_dump_json(),
                ),
            )
            for capture, path in evidence_paths:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO evidence(
                        digest, path, source_url, media_type
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (capture.digest, str(path), str(capture.url), capture.media_type),
                )
        return application_id

    def get_application(self, application_id: ApplicationId) -> StoredApplication:
        """Return current successful content plus latest completeness."""
        row = self._connection.execute(
            "SELECT authority_id, reference FROM applications WHERE id = ?",
            (application_id,),
        ).fetchone()
        if row is None:
            raise KeyError(application_id)
        application = _ApplicationSection.model_validate_json(
            self._section_payload(application_id, "application")
        )
        documents = _DOCUMENTS.validate_json(
            self._section_payload(application_id, "documents")
        )
        comments = _COMMENTS.validate_json(
            self._section_payload(application_id, "comments")
        )
        completeness_row = next(
            self._connection.execute(
                """
                SELECT completeness_json FROM observations
                WHERE application_id = ? ORDER BY id DESC LIMIT 1
                """,
                (application_id,),
            )
        )
        return StoredApplication(
            id=application_id,
            authority_id=AuthorityId(row["authority_id"]),
            reference=row["reference"],
            proposal=application.proposal,
            status=application.status,
            documents=documents,
            comments=comments,
            completeness=_COMPLETENESS.validate_json(
                completeness_row["completeness_json"]
            ),
        )

    def discovery_state(self, authority_id: AuthorityId) -> DiscoveryState:
        """Return the durable queue and matching checkpoint."""
        references = tuple(
            row["reference"]
            for row in self._connection.execute(
                """
                SELECT reference FROM discovery_queue
                WHERE authority_id = ? ORDER BY reference
                """,
                (authority_id,),
            )
        )
        row = self._connection.execute(
            """
            SELECT schema_version, payload_json FROM checkpoints
            WHERE authority_id = ?
            """,
            (authority_id,),
        ).fetchone()
        checkpoint = (
            None
            if row is None
            else StoredCheckpoint(
                schema_version=row["schema_version"],
                payload_json=row["payload_json"],
            )
        )
        return DiscoveryState(references=references, checkpoint=checkpoint)

    def semantic_version_count(
        self,
        application_id: ApplicationId,
        section: Literal["application", "documents", "comments"],
    ) -> int:
        """Count semantic versions for one section."""
        row = next(
            self._connection.execute(
                """
                SELECT COUNT(*) AS count FROM semantic_versions
                WHERE application_id = ? AND section = ?
                """,
                (application_id, section),
            )
        )
        return int(row["count"])

    def _migrate(self) -> None:
        migration = files("yimby.migrations").joinpath("001_initial.sql").read_text()
        self._connection.executescript(migration)

    def _commit_section(
        self,
        application_id: ApplicationId,
        section: str,
        payload: str,
        normaliser_version: str,
    ) -> None:
        semantic_hash = sha256(payload.encode()).hexdigest()
        self._connection.execute(
            """
            INSERT OR IGNORE INTO semantic_versions(
                application_id, section, semantic_hash, payload_json, normaliser_version
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                application_id,
                section,
                semantic_hash,
                payload,
                normaliser_version,
            ),
        )
        row = next(
            self._connection.execute(
                """
                SELECT id FROM semantic_versions
                WHERE application_id = ? AND section = ? AND semantic_hash = ?
                    AND normaliser_version = ?
                """,
                (application_id, section, semantic_hash, normaliser_version),
            )
        )
        self._connection.execute(
            """
            INSERT INTO section_current(application_id, section, version_id)
            VALUES (?, ?, ?)
            ON CONFLICT(application_id, section) DO UPDATE SET
                version_id = excluded.version_id
            """,
            (application_id, section, row["id"]),
        )

    def _section_payload(self, application_id: ApplicationId, section: str) -> str:
        row = self._connection.execute(
            """
            SELECT semantic_versions.payload_json
            FROM section_current
            JOIN semantic_versions ON semantic_versions.id = section_current.version_id
            WHERE section_current.application_id = ? AND section_current.section = ?
            """,
            (application_id, section),
        ).fetchone()
        if row is None:
            return json.dumps([])
        return str(row["payload_json"])
