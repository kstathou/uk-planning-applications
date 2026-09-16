# Copyright (c) 2026 Kostas Stathoulopoulos

"""SQLite persistence for durable collection and local operations."""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from importlib.resources import files
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import HttpUrl, TypeAdapter

from yimby.domain import (
    ApplicationEvent,
    ApplicationId,
    ApplicationLocation,
    ApplicationMetadata,
    ApplicationRelationship,
    ApplicationView,
    AuthorityCapabilities,
    AuthorityId,
    AuthorityManifest,
    AuthorityOperationalState,
    AuthorityReferenceSets,
    CapabilityState,
    CollectedObservation,
    CommentRecord,
    Completeness,
    DiscoveryState,
    DocumentRecord,
    DurableDiscoveryBatch,
    EvidenceCapture,
    EvidenceDigest,
    EvidenceIntegrityIssue,
    EvidenceIntegrityReport,
    FrozenModel,
    NormalisedObservation,
    QualificationSnapshot,
    RetainedNativeRecord,
    RetryItem,
    RunCostSnapshot,
    RunMetrics,
    RunOutcome,
    RunStatus,
    SourceId,
    SourceReference,
    StoredApplication,
    StoredCheckpoint,
    TransportMode,
    Wgs84Coordinate,
)
from yimby.evidence import EvidenceIntegrityError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from yimby.evidence import EvidenceStore

_DOCUMENTS = TypeAdapter(tuple[DocumentRecord, ...])
_COMMENTS = TypeAdapter(tuple[CommentRecord, ...])
_COMPLETENESS = TypeAdapter(Completeness)


class _RetainedEvidenceCapture(FrozenModel):
    digest: EvidenceDigest
    source_url: HttpUrl
    media_type: str


_RETAINED_EVIDENCE = TypeAdapter(tuple[_RetainedEvidenceCapture, ...])


class _ApplicationSection(FrozenModel):
    proposal: str
    status: str


class RetainedEvidenceRegistration(FrozenModel):
    """One application's durable link to a registered evidence object."""

    application_id: ApplicationId
    digest: EvidenceDigest
    path: str


class EvidenceRegistrationAudit(FrozenModel):
    """Authority-scoped reconciliation facts for retained native evidence."""

    application_count: int
    applications_with_evidence: int
    registrations: tuple[RetainedEvidenceRegistration, ...]
    missing_digests: tuple[EvidenceDigest, ...]


class SqliteStore:
    """Own the only SQLite writer connection for a collection runtime."""

    def __init__(self, path: Path, evidence: EvidenceStore) -> None:
        """Open the writer, enable SQLite safety settings, and migrate."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.evidence_root = evidence.root
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._evidence = evidence
        self._migrate()

    def close(self) -> None:
        """Close the writer connection."""
        self._connection.close()

    def register_authorities(self, manifests: Iterable[AuthorityManifest]) -> None:
        """Persist registry ownership without overwriting collection state."""
        now = datetime.now(UTC).isoformat()
        with self._connection:
            for manifest in manifests:
                self._connection.execute(
                    """
                    INSERT INTO authorities(
                        authority_id, name, kind, implementation_status,
                        transport_mode, capabilities_json, source_manifest_json,
                        last_success_at, updated_at, live_readiness, live_reason,
                        live_evidence_json, live_transport
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                    ON CONFLICT(authority_id) DO UPDATE SET
                        name = excluded.name,
                        kind = excluded.kind,
                        source_manifest_json = excluded.source_manifest_json,
                        live_readiness = excluded.live_readiness,
                        live_reason = excluded.live_reason,
                        live_evidence_json = excluded.live_evidence_json,
                        live_transport = excluded.live_transport,
                        updated_at = excluded.updated_at
                    """,
                    (
                        manifest.id,
                        manifest.name,
                        manifest.kind,
                        "fixture-ready",
                        TransportMode.NOT_RUN,
                        manifest.capabilities.model_dump_json(),
                        manifest.model_dump_json(),
                        now,
                        manifest.live_status.readiness,
                        manifest.live_status.reason,
                        json.dumps(manifest.live_status.evidence),
                        manifest.live_status.transport,
                    ),
                )

    def begin_run(self, authority_id: AuthorityId) -> str:
        """Create a collection run with a durable running state."""
        run_id = str(uuid4())
        with self._connection:
            self._connection.execute(
                "INSERT INTO runs(id, authority_id, started_at) VALUES (?, ?, ?)",
                (run_id, authority_id, datetime.now(UTC).isoformat()),
            )
            self._connection.execute(
                "INSERT INTO run_details(run_id, status) VALUES (?, ?)",
                (run_id, RunStatus.RUNNING),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        authority_id: AuthorityId,
        outcome: RunOutcome,
    ) -> None:
        """Finish one run and update authority freshness atomically."""
        now = datetime.now(UTC).isoformat()
        with self._connection:
            self._connection.execute(
                """
                UPDATE run_details SET
                    status = ?, finished_at = ?, request_count = ?,
                    transferred_bytes = ?, duration_ms = ?,
                    browser_time_ms = ?, storage_growth_bytes = ?,
                    failure_message = ?
                WHERE run_id = ?
                """,
                (
                    outcome.status,
                    now,
                    outcome.metrics.request_count,
                    outcome.metrics.transferred_bytes,
                    outcome.metrics.duration_ms,
                    outcome.metrics.browser_time_ms,
                    outcome.metrics.storage_growth_bytes,
                    outcome.failure_message,
                    run_id,
                ),
            )
            self._connection.execute(
                """
                UPDATE authorities SET transport_mode = ?, updated_at = ?
                WHERE authority_id = ?
                """,
                (outcome.transport_mode, now, authority_id),
            )
            if outcome.status == RunStatus.SUCCEEDED:
                self._connection.execute(
                    """
                    UPDATE authorities SET last_success_at = ?
                    WHERE authority_id = ?
                    """,
                    (now, authority_id),
                )

    def commit_discovery(
        self,
        run_id: str,
        authority_id: AuthorityId,
        batch: DurableDiscoveryBatch,
    ) -> None:
        """Retain evidence, queue references, and advance the checkpoint atomically."""
        evidence_paths = [
            (capture, self._evidence.put(capture)) for capture in batch.evidence
        ]
        with self._connection:
            for capture, path in evidence_paths:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO evidence(
                        digest, path, source_url, media_type
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        capture.digest,
                        self._evidence.relative_path(path),
                        str(capture.url),
                        capture.media_type,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO discovery_evidence(
                        authority_id, run_id, digest
                    ) VALUES (?, ?, ?)
                    """,
                    (authority_id, run_id, capture.digest),
                )
            for reference in batch.references:
                self._connection.execute(
                    """
                    INSERT INTO discovery_queue(
                        authority_id, source_id, reference, locator,
                        first_run_id, last_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, reference) DO UPDATE SET
                        locator = COALESCE(excluded.locator, discovery_queue.locator),
                        last_run_id = excluded.last_run_id
                    """,
                    (
                        authority_id,
                        reference.source_id,
                        reference.reference,
                        reference.locator,
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
        """Persist evidence, native input, and semantic section transitions."""
        normalised = collected.normalised
        application_id = self._application_id(normalised)
        evidence_paths = [
            (capture, self._evidence.put(capture)) for capture in collected.evidence
        ]
        native_hash = sha256(collected.native_json.encode()).hexdigest()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO applications(
                    id, authority_id, source_id, reference, locator
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_id, reference) DO UPDATE SET
                    locator = COALESCE(excluded.locator, applications.locator)
                """,
                (
                    application_id,
                    normalised.authority_id,
                    normalised.reference.source_id,
                    normalised.reference.reference,
                    normalised.reference.locator,
                ),
            )
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
            self._commit_normalised(application_id, normalised)
            observation_id = next(
                self._connection.execute(
                    """
                    INSERT INTO observations(
                        run_id, application_id, observed_at, completeness_json
                    ) VALUES (?, ?, ?, ?)
                    RETURNING id
                    """,
                    (
                        run_id,
                        application_id,
                        collected.observed_at.isoformat(),
                        normalised.completeness.model_dump_json(),
                    ),
                )
            )["id"]
            self._record_observed_versions(
                observation_id,
                application_id,
                native_hash,
            )
            for capture, path in evidence_paths:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO evidence(
                        digest, path, source_url, media_type
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        capture.digest,
                        self._evidence.relative_path(path),
                        str(capture.url),
                        capture.media_type,
                    ),
                )
            self._connection.execute(
                """
                INSERT INTO native_rebuild_inputs(
                    application_id, authority_id, source_id, reference, schema_name,
                    locator, payload_json, observed_at, completeness_json,
                    evidence_digests_json, evidence_captures_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(application_id) DO UPDATE SET
                    schema_name = excluded.schema_name,
                    locator = COALESCE(excluded.locator, native_rebuild_inputs.locator),
                    payload_json = excluded.payload_json,
                    observed_at = excluded.observed_at,
                    completeness_json = excluded.completeness_json,
                    evidence_digests_json = excluded.evidence_digests_json,
                    evidence_captures_json = excluded.evidence_captures_json
                """,
                (
                    application_id,
                    normalised.authority_id,
                    normalised.reference.source_id,
                    normalised.reference.reference,
                    collected.native_schema,
                    normalised.reference.locator,
                    collected.native_json,
                    collected.observed_at.isoformat(),
                    normalised.completeness.model_dump_json(),
                    json.dumps(
                        [str(capture.digest) for capture in collected.evidence],
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        [
                            {
                                "digest": str(capture.digest),
                                "source_url": str(capture.url),
                                "media_type": capture.media_type,
                            }
                            for capture in collected.evidence
                        ],
                        separators=(",", ":"),
                    ),
                ),
            )
            self._set_refresh_schedule(
                application_id,
                normalised,
                collected.observed_at,
            )
            self._update_observed_capabilities(normalised)
        return application_id

    def commit_rebuild(
        self,
        application_id: ApplicationId,
        normalised: NormalisedObservation,
    ) -> None:
        """Publish rebuilt semantics while retaining every older version."""
        with self._connection:
            self._commit_normalised(application_id, normalised)
            self._connection.execute(
                """
                INSERT INTO normalisation_rebuilds(
                    application_id, normaliser_version, rebuilt_at
                ) VALUES (?, ?, ?)
                """,
                (
                    application_id,
                    normalised.normaliser_version,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def retained_native_records(self) -> tuple[RetainedNativeRecord, ...]:
        """Return rebuild inputs with evidence rehydrated from local storage."""
        retained: list[RetainedNativeRecord] = []
        rows = self._connection.execute(
            "SELECT * FROM native_rebuild_inputs ORDER BY application_id"
        )
        for row in rows:
            captures = []
            associations = _RETAINED_EVIDENCE.validate_json(
                row["evidence_captures_json"]
            )
            capture_inputs: tuple[tuple[EvidenceDigest, str | None, str | None], ...]
            if associations:
                capture_inputs = tuple(
                    (
                        capture.digest,
                        str(capture.source_url),
                        capture.media_type,
                    )
                    for capture in associations
                )
            else:
                capture_inputs = tuple(
                    (EvidenceDigest(digest_value), None, None)
                    for digest_value in json.loads(row["evidence_digests_json"])
                )
            for digest, captured_url, captured_media_type in capture_inputs:
                evidence = self._connection.execute(
                    """
                    SELECT path, source_url, media_type FROM evidence
                    WHERE digest = ?
                    """,
                    (digest,),
                ).fetchone()
                if evidence is None:
                    raise KeyError(digest)
                source_url = (
                    captured_url if captured_url is not None else evidence["source_url"]
                )
                media_type = (
                    captured_media_type
                    if captured_media_type is not None
                    else evidence["media_type"]
                )
                captures.append(
                    self._evidence.read_capture(
                        digest,
                        evidence["path"],
                        source_url,
                        media_type,
                    )
                )
            retained.append(
                RetainedNativeRecord(
                    application_id=ApplicationId(row["application_id"]),
                    authority_id=AuthorityId(row["authority_id"]),
                    reference=SourceReference(
                        source_id=SourceId(row["source_id"]),
                        reference=row["reference"],
                        locator=row["locator"],
                    ),
                    native_schema=row["schema_name"],
                    native_json=row["payload_json"],
                    observed_at=datetime.fromisoformat(row["observed_at"]),
                    completeness=_COMPLETENESS.validate_json(row["completeness_json"]),
                    evidence=tuple(captures),
                )
            )
        return tuple(retained)

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

    def application_view(self, application_id: ApplicationId) -> ApplicationView:
        """Return current sections with metadata, version, and suppression state."""
        application = self.get_application(application_id)
        detail = self._connection.execute(
            "SELECT * FROM application_details WHERE application_id = ?",
            (application_id,),
        ).fetchone()
        location_row = self._connection.execute(
            "SELECT * FROM application_locations WHERE application_id = ?",
            (application_id,),
        ).fetchone()
        aliases = tuple(
            row["alias"]
            for row in self._connection.execute(
                """
                SELECT alias FROM application_aliases
                WHERE application_id = ? ORDER BY alias
                """,
                (application_id,),
            )
        )
        events = tuple(
            self._event_from_row(row)
            for row in self._connection.execute(
                """
                SELECT event_type, event_at, details FROM application_events
                WHERE application_id = ? ORDER BY event_at, id
                """,
                (application_id,),
            )
        )
        relationships = tuple(
            ApplicationRelationship(
                related_reference=row["related_reference"],
                relationship_type=row["relationship_type"],
            )
            for row in self._connection.execute(
                """
                SELECT related_reference, relationship_type
                FROM application_relationships
                WHERE application_id = ?
                ORDER BY relationship_type, related_reference
                """,
                (application_id,),
            )
        )
        version_row = next(
            self._connection.execute(
                """
                SELECT semantic_versions.normaliser_version
                FROM section_current
                JOIN semantic_versions
                    ON semantic_versions.id = section_current.version_id
                WHERE section_current.application_id = ?
                    AND section_current.section = 'application'
                """,
                (application_id,),
            )
        )
        observed_row = next(
            self._connection.execute(
                """
                SELECT observed_at FROM observations
                WHERE application_id = ? ORDER BY id DESC LIMIT 1
                """,
                (application_id,),
            )
        )
        suppression = self._connection.execute(
            """
            SELECT suppressed FROM suppression_corrections
            WHERE application_id = ?
            """,
            (application_id,),
        ).fetchone()
        metadata = self._metadata_from_rows(
            detail,
            location_row,
            aliases,
            events,
            relationships,
        )
        return ApplicationView(
            application=application,
            metadata=metadata,
            normaliser_version=version_row["normaliser_version"],
            observed_at=datetime.fromisoformat(observed_row["observed_at"]),
            suppressed=suppression is not None and bool(suppression["suppressed"]),
        )

    def application_views(self) -> tuple[ApplicationView, ...]:
        """Return unsuppressed current records in deterministic identifier order."""
        views = (
            self.application_view(ApplicationId(row["id"]))
            for row in self._connection.execute(
                "SELECT id FROM applications ORDER BY id"
            )
        )
        return tuple(view for view in views if not view.suppressed)

    def authority_application_ids(
        self,
        authority_id: AuthorityId,
    ) -> tuple[ApplicationId, ...]:
        """Return every authority application, including suppressed records."""
        return tuple(
            ApplicationId(row["id"])
            for row in self._connection.execute(
                """
                SELECT id FROM applications
                WHERE authority_id = ? ORDER BY id
                """,
                (authority_id,),
            )
        )

    def authority_semantic_state(
        self,
        authority_id: AuthorityId,
    ) -> tuple[tuple[str, str, str], ...]:
        """Return current section hashes and completeness for idempotence proof."""
        rows = self._connection.execute(
            """
            SELECT application.id AS application_id,
                section_current.section AS state_kind,
                semantic_versions.semantic_hash AS state_value
            FROM applications AS application
            JOIN section_current
                ON section_current.application_id = application.id
            JOIN semantic_versions
                ON semantic_versions.id = section_current.version_id
            WHERE application.authority_id = ?
            UNION ALL
            SELECT application.id AS application_id,
                'completeness' AS state_kind,
                observation.completeness_json AS state_value
            FROM applications AS application
            JOIN observations AS observation
                ON observation.application_id = application.id
            WHERE application.authority_id = ?
                AND observation.id = (
                    SELECT MAX(latest.id) FROM observations AS latest
                    WHERE latest.application_id = application.id
                )
            ORDER BY application_id, state_kind
            """,
            (authority_id, authority_id),
        )
        return tuple(
            (row["application_id"], row["state_kind"], row["state_value"])
            for row in rows
        )

    def search_applications(self, query: str) -> tuple[ApplicationView, ...]:
        """Search current local applications without exposing native payloads."""
        needle = query.casefold()
        return tuple(
            view
            for view in self.application_views()
            if needle
            in " ".join(
                (
                    view.application.reference,
                    view.application.proposal,
                    view.metadata.address or "",
                )
            ).casefold()
        )

    def discovery_state(self, authority_id: AuthorityId) -> DiscoveryState:
        """Return the durable queue and matching checkpoint."""
        queue_rows = tuple(
            self._connection.execute(
                """
                SELECT source_id, reference, locator FROM discovery_queue
                WHERE authority_id = ? ORDER BY reference
                """,
                (authority_id,),
            )
        )
        references = tuple(row["reference"] for row in queue_rows)
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
        return DiscoveryState(
            references=references,
            queued=tuple(
                SourceReference(
                    source_id=SourceId(row["source_id"]),
                    reference=row["reference"],
                    locator=row["locator"],
                )
                for row in queue_rows
            ),
            checkpoint=checkpoint,
        )

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

    def enqueue_retry(
        self,
        authority_id: AuthorityId,
        reference: SourceReference,
        error: str,
    ) -> None:
        """Make a failed detail reference durably retryable."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO retry_queue(
                    authority_id, source_id, reference, locator, attempts,
                    next_attempt_at, last_error, status
                ) VALUES (?, ?, ?, ?, 1, ?, ?, 'pending')
                ON CONFLICT(authority_id, source_id, reference) DO UPDATE SET
                    locator = COALESCE(excluded.locator, retry_queue.locator),
                    attempts = retry_queue.attempts + 1,
                    next_attempt_at = excluded.next_attempt_at,
                    last_error = excluded.last_error,
                    status = 'pending'
                """,
                (
                    authority_id,
                    reference.source_id,
                    reference.reference,
                    reference.locator,
                    datetime.now(UTC).isoformat(),
                    error,
                ),
            )

    def mark_retry_succeeded(
        self,
        authority_id: AuthorityId,
        reference: SourceReference,
    ) -> None:
        """Close a pending retry after a successful detail collection."""
        with self._connection:
            self._connection.execute(
                """
                UPDATE retry_queue SET status = 'succeeded'
                WHERE authority_id = ? AND source_id = ? AND reference = ?
                """,
                (authority_id, reference.source_id, reference.reference),
            )

    def retry_items(self) -> tuple[RetryItem, ...]:
        """Return retry state in stable authority/reference order."""
        return tuple(
            RetryItem(
                authority_id=AuthorityId(row["authority_id"]),
                reference=SourceReference(
                    source_id=SourceId(row["source_id"]),
                    reference=row["reference"],
                    locator=row["locator"],
                ),
                attempts=row["attempts"],
                last_error=row["last_error"],
                status=row["status"],
            )
            for row in self._connection.execute(
                """
                SELECT * FROM retry_queue
                ORDER BY authority_id, source_id, reference
                """
            )
        )

    def collection_work(
        self,
        authority_id: AuthorityId,
        now: datetime,
    ) -> tuple[SourceReference, ...]:
        """Return uncollected, retryable, and due references without duplicates."""
        rows = (
            self._connection.execute(
                """
                SELECT queue.source_id, queue.reference, queue.locator
                FROM discovery_queue AS queue
                LEFT JOIN applications AS application
                    ON application.source_id = queue.source_id
                    AND application.reference = queue.reference
                WHERE queue.authority_id = ? AND application.id IS NULL
                ORDER BY queue.source_id, queue.reference
                """,
                (authority_id,),
            ),
            self._connection.execute(
                """
                SELECT source_id, reference, locator FROM retry_queue
                WHERE authority_id = ? AND status = 'pending'
                    AND next_attempt_at <= ?
                ORDER BY source_id, reference
                """,
                (authority_id, now.isoformat()),
            ),
            self._connection.execute(
                """
                SELECT application.source_id, application.reference,
                    application.locator
                FROM applications AS application
                JOIN refresh_schedules AS schedule
                    ON schedule.application_id = application.id
                WHERE application.authority_id = ?
                    AND schedule.next_refresh_at <= ?
                ORDER BY application.source_id, application.reference
                """,
                (authority_id, now.isoformat()),
            ),
        )
        work: dict[tuple[str, str], SourceReference] = {}
        for result in rows:
            for row in result:
                key = (row["source_id"], row["reference"])
                candidate = SourceReference(
                    source_id=SourceId(row["source_id"]),
                    reference=row["reference"],
                    locator=row["locator"],
                )
                current = work.get(key)
                if current is None or (
                    current.locator is None and candidate.locator is not None
                ):
                    work[key] = candidate
        return tuple(work.values())

    def record_failure(
        self,
        authority_id: AuthorityId,
        run_id: str,
        code: str,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        """Retain a sanitised failure diagnostic for thirty days."""
        occurred_at = datetime.now(UTC)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO failure_diagnostics(
                    authority_id, run_id, occurred_at, code, message,
                    retryable, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    authority_id,
                    run_id,
                    occurred_at.isoformat(),
                    code,
                    message,
                    retryable,
                    (occurred_at + timedelta(days=30)).isoformat(),
                ),
            )

    def prune_expired_diagnostics(self, now: datetime) -> int:
        """Remove diagnostics after their documented retention window."""
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM failure_diagnostics WHERE expires_at < ?",
                (now.isoformat(),),
            )
        return cursor.rowcount

    def set_refresh_schedule(
        self,
        application_id: ApplicationId,
        next_refresh_at: datetime,
        cadence: str,
        reason: str,
    ) -> None:
        """Upsert one application's next refresh decision."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO refresh_schedules(
                    application_id, next_refresh_at, cadence, reason
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(application_id) DO UPDATE SET
                    next_refresh_at = excluded.next_refresh_at,
                    cadence = excluded.cadence,
                    reason = excluded.reason
                """,
                (application_id, next_refresh_at.isoformat(), cadence, reason),
            )

    def set_suppression(
        self,
        application_id: ApplicationId,
        *,
        suppressed: bool,
        reason: str,
    ) -> None:
        """Apply or lift a reviewed export suppression correction."""
        self.get_application(application_id)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO suppression_corrections(
                    application_id, suppressed, reason, corrected_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(application_id) DO UPDATE SET
                    suppressed = excluded.suppressed,
                    reason = excluded.reason,
                    corrected_at = excluded.corrected_at
                """,
                (
                    application_id,
                    suppressed,
                    reason,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def authority_states(
        self, now: datetime | None = None
    ) -> tuple[AuthorityOperationalState, ...]:
        """Return registry, freshness, failure, and backlog state."""
        observed_now = datetime.now(UTC) if now is None else now
        states = []
        for row in self._connection.execute("SELECT * FROM authorities ORDER BY rowid"):
            last_success_at = (
                None
                if row["last_success_at"] is None
                else datetime.fromisoformat(row["last_success_at"])
            )
            freshness = (
                None
                if last_success_at is None
                else max(0, (observed_now - last_success_at).days)
            )
            failure_count = int(
                next(
                    self._connection.execute(
                        """
                        SELECT COUNT(*) AS count FROM failure_diagnostics
                        WHERE authority_id = ?
                        """,
                        (row["authority_id"],),
                    )
                )["count"]
            )
            backlog_count = int(
                next(
                    self._connection.execute(
                        """
                        SELECT COUNT(*) AS count FROM retry_queue
                        WHERE authority_id = ? AND status = 'pending'
                        """,
                        (row["authority_id"],),
                    )
                )["count"]
            )
            states.append(
                AuthorityOperationalState(
                    manifest=AuthorityManifest.model_validate_json(
                        row["source_manifest_json"]
                    ).model_copy(
                        update={
                            "capabilities": AuthorityCapabilities.model_validate_json(
                                row["capabilities_json"]
                            )
                        }
                    ),
                    implementation_status=row["implementation_status"],
                    transport_mode=TransportMode(row["transport_mode"]),
                    last_success_at=last_success_at,
                    freshness_days=freshness,
                    failure_count=failure_count,
                    backlog_count=backlog_count,
                )
            )
        return tuple(states)

    def run_statuses(self) -> tuple[RunStatus, ...]:
        """Return durable run states in creation order."""
        return tuple(
            RunStatus(row["status"])
            for row in self._connection.execute(
                "SELECT status FROM run_details ORDER BY rowid"
            )
        )

    def run_costs(self, authority_id: AuthorityId) -> tuple[RunCostSnapshot, ...]:
        """Return one authority's durable run costs in creation order."""
        return tuple(
            RunCostSnapshot(
                status=RunStatus(row["status"]),
                request_count=row["request_count"],
                transferred_bytes=row["transferred_bytes"],
            )
            for row in self._connection.execute(
                """
                SELECT detail.status, detail.request_count, detail.transferred_bytes
                FROM run_details AS detail
                JOIN runs AS run ON run.id = detail.run_id
                WHERE run.authority_id = ?
                ORDER BY detail.rowid
                """,
                (authority_id,),
            )
        )

    def qualification_snapshot(
        self,
        authority_id: AuthorityId,
    ) -> QualificationSnapshot:
        """Return one authority's durable qualification counts."""
        counts = next(
            self._connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM applications
                        WHERE authority_id = :authority_id) AS applications,
                    (SELECT COUNT(*) FROM discovery_queue
                        WHERE authority_id = :authority_id) AS discovered_references,
                    (SELECT COUNT(*) FROM native_versions AS native
                        JOIN applications AS application
                            ON application.id = native.application_id
                        WHERE application.authority_id = :authority_id
                    ) AS native_versions,
                    (SELECT COUNT(*) FROM semantic_versions AS semantic
                        JOIN applications AS application
                            ON application.id = semantic.application_id
                        WHERE application.authority_id = :authority_id
                            AND semantic.section = 'application'
                    ) AS application_versions,
                    (SELECT COUNT(*) FROM semantic_versions AS semantic
                        JOIN applications AS application
                            ON application.id = semantic.application_id
                        WHERE application.authority_id = :authority_id
                            AND semantic.section = 'documents'
                    ) AS document_versions,
                    (SELECT COUNT(*) FROM semantic_versions AS semantic
                        JOIN applications AS application
                            ON application.id = semantic.application_id
                        WHERE application.authority_id = :authority_id
                            AND semantic.section = 'comments'
                    ) AS comment_versions,
                    (SELECT COUNT(*) FROM retry_queue
                        WHERE authority_id = :authority_id AND status = 'pending'
                    ) AS pending_retries,
                    (SELECT COUNT(*) FROM applications AS application
                        LEFT JOIN section_current AS current
                            ON current.application_id = application.id
                            AND current.section = 'application'
                        WHERE application.authority_id = :authority_id
                            AND current.application_id IS NULL
                    ) AS unmapped_records
                """,
                {"authority_id": authority_id},
            )
        )
        completeness_rows = self._connection.execute(
            """
            SELECT observation.completeness_json
            FROM observations AS observation
            JOIN applications AS application
                ON application.id = observation.application_id
            WHERE application.authority_id = ?
                AND observation.id = (
                    SELECT MAX(latest.id) FROM observations AS latest
                    WHERE latest.application_id = observation.application_id
                )
            ORDER BY observation.application_id
            """,
            (authority_id,),
        )
        failed_sections = 0
        for row in completeness_rows:
            completeness = _COMPLETENESS.validate_json(row["completeness_json"])
            failed_sections += sum(
                state.kind == "failed"
                for state in (
                    completeness.application,
                    completeness.documents,
                    completeness.comments,
                )
            )
        return QualificationSnapshot(
            authority_id=authority_id,
            applications=counts["applications"],
            discovered_references=counts["discovered_references"],
            native_versions=counts["native_versions"],
            application_versions=counts["application_versions"],
            document_versions=counts["document_versions"],
            comment_versions=counts["comment_versions"],
            pending_retries=counts["pending_retries"],
            failed_sections=failed_sections,
            unmapped_records=counts["unmapped_records"],
        )

    def authority_completeness(
        self,
        authority_id: AuthorityId,
    ) -> tuple[Completeness, ...]:
        """Return each authority application's latest section states."""
        rows = self._connection.execute(
            """
            SELECT observation.completeness_json
            FROM observations AS observation
            JOIN applications AS application
                ON application.id = observation.application_id
            WHERE application.authority_id = ?
                AND observation.id = (
                    SELECT MAX(latest.id) FROM observations AS latest
                    WHERE latest.application_id = observation.application_id
                )
            ORDER BY observation.application_id
            """,
            (authority_id,),
        )
        return tuple(
            _COMPLETENESS.validate_json(row["completeness_json"]) for row in rows
        )

    def authority_reference_sets(
        self,
        authority_id: AuthorityId,
    ) -> AuthorityReferenceSets:
        """Return independent discovery, application, and rebuild identities."""

        def references(table: str) -> tuple[SourceReference, ...]:
            rows = self._connection.execute(
                f"""
                SELECT source_id, reference, locator FROM {table}
                WHERE authority_id = ? ORDER BY source_id, reference
                """,  # noqa: S608 - Table names are fixed below, never caller supplied.
                (authority_id,),
            )
            return tuple(
                SourceReference(
                    source_id=SourceId(row["source_id"]),
                    reference=row["reference"],
                    locator=row["locator"],
                )
                for row in rows
            )

        return AuthorityReferenceSets(
            discovery=references("discovery_queue"),
            applications=references("applications"),
            rebuild_inputs=references("native_rebuild_inputs"),
        )

    def evidence_integrity(
        self,
        authority_id: AuthorityId,
    ) -> EvidenceIntegrityReport:
        """Verify authority-linked evidence registration, gzip bodies, and digests."""
        issues: list[EvidenceIntegrityIssue] = []
        application_ids = tuple(
            row["id"]
            for row in self._connection.execute(
                """
                SELECT id FROM applications
                WHERE authority_id = ? ORDER BY id
                """,
                (authority_id,),
            )
        )
        rebuild_rows = {
            row["application_id"]: row
            for row in self._connection.execute(
                """
                SELECT application_id, evidence_digests_json
                FROM native_rebuild_inputs
                WHERE authority_id = ? ORDER BY application_id
                """,
                (authority_id,),
            )
        }
        linked_digests: set[str] = set()
        for application_id in application_ids:
            row = rebuild_rows.get(application_id)
            if row is None:
                issues.append(
                    EvidenceIntegrityIssue(
                        digest=None,
                        code="application-without-rebuild-input",
                    )
                )
                continue
            digests = tuple(
                str(value) for value in json.loads(row["evidence_digests_json"])
            )
            if not digests:
                issues.append(
                    EvidenceIntegrityIssue(
                        digest=None,
                        code="application-without-evidence",
                    )
                )
            linked_digests.update(digests)
        linked_digests.update(
            row["digest"]
            for row in self._connection.execute(
                """
                SELECT DISTINCT digest FROM discovery_evidence
                WHERE authority_id = ?
                """,
                (authority_id,),
            )
        )

        manifest: list[tuple[str, str, int, int]] = []
        captures_checked = 0
        compressed_bytes = 0
        uncompressed_bytes = 0
        for digest in sorted(linked_digests):
            captures_checked += 1
            row = self._connection.execute(
                "SELECT path FROM evidence WHERE digest = ?",
                (digest,),
            ).fetchone()
            if row is None:
                issues.append(
                    EvidenceIntegrityIssue(
                        digest=digest,
                        code="unregistered-digest",
                    )
                )
                continue
            stored_path = str(row["path"])
            expected_path = f"{digest[:2]}/{digest}.gz"
            if stored_path != expected_path:
                issues.append(
                    EvidenceIntegrityIssue(digest=digest, code="path-mismatch")
                )
                continue
            path = self.evidence_root / stored_path
            if not path.is_file():
                issues.append(
                    EvidenceIntegrityIssue(digest=digest, code="missing-path")
                )
                continue
            compressed = path.read_bytes()
            compressed_bytes += len(compressed)
            try:
                body = gzip.decompress(compressed)
            except (gzip.BadGzipFile, EOFError, OSError):
                issues.append(
                    EvidenceIntegrityIssue(digest=digest, code="invalid-gzip")
                )
                continue
            uncompressed_bytes += len(body)
            manifest.append((digest, stored_path, len(compressed), len(body)))
            if sha256(body).hexdigest() != digest:
                issues.append(
                    EvidenceIntegrityIssue(digest=digest, code="digest-mismatch")
                )
        manifest_payload = json.dumps(
            manifest,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return EvidenceIntegrityReport(
            captures_checked=captures_checked,
            compressed_bytes=compressed_bytes,
            uncompressed_bytes=uncompressed_bytes,
            manifest_sha256=sha256(manifest_payload).hexdigest(),
            issues=tuple(issues),
        )

    def discovery_evidence_count(self, authority_id: AuthorityId) -> int:
        """Count distinct discovery captures retained for one authority."""
        row = next(
            self._connection.execute(
                """
                SELECT COUNT(DISTINCT digest) AS count
                FROM discovery_evidence
                WHERE authority_id = ?
                """,
                (authority_id,),
            )
        )
        return int(row["count"])

    def discovery_evidence_captures(
        self,
        authority_id: AuthorityId,
    ) -> tuple[EvidenceCapture, ...]:
        """Rehydrate distinct discovery captures linked to one authority."""
        rows = self._connection.execute(
            """
            SELECT DISTINCT evidence.digest, evidence.path, evidence.source_url,
                evidence.media_type
            FROM discovery_evidence
            JOIN evidence ON evidence.digest = discovery_evidence.digest
            WHERE discovery_evidence.authority_id = ?
            ORDER BY evidence.digest
            """,
            (authority_id,),
        )
        return tuple(
            self._evidence.read_capture(
                EvidenceDigest(row["digest"]),
                row["path"],
                row["source_url"],
                row["media_type"],
            )
            for row in rows
        )

    def metrics_totals(self) -> RunMetrics:
        """Aggregate completed collection costs for dashboard display."""
        row = next(
            self._connection.execute(
                """
                SELECT
                    COALESCE(SUM(request_count), 0) AS request_count,
                    COALESCE(SUM(transferred_bytes), 0) AS transferred_bytes,
                    COALESCE(SUM(duration_ms), 0) AS duration_ms,
                    COALESCE(SUM(browser_time_ms), 0) AS browser_time_ms,
                    COALESCE(SUM(storage_growth_bytes), 0) AS storage_growth_bytes
                FROM run_details
                """
            )
        )
        return RunMetrics(
            request_count=row["request_count"],
            transferred_bytes=row["transferred_bytes"],
            duration_ms=row["duration_ms"],
            browser_time_ms=row["browser_time_ms"],
            storage_growth_bytes=row["storage_growth_bytes"],
        )

    def application_count(self) -> int:
        """Count locally known applications."""
        row = next(
            self._connection.execute("SELECT COUNT(*) AS count FROM applications")
        )
        return int(row["count"])

    def observed_change_count(self) -> int:
        """Count source-observed section transitions, including reversions."""
        row = next(
            self._connection.execute(
                """
                SELECT COUNT(*) AS changes
                FROM (
                    SELECT version.semantic_hash,
                        LAG(version.semantic_hash) OVER (
                            PARTITION BY observation.application_id, observed.section
                            ORDER BY observed.observation_id
                        ) AS previous_semantic_hash
                    FROM observation_section_versions AS observed
                    JOIN observations AS observation
                        ON observation.id = observed.observation_id
                    JOIN semantic_versions AS version
                        ON version.id = observed.version_id
                )
                WHERE previous_semantic_hash IS NOT NULL
                    AND semantic_hash != previous_semantic_hash
                """
            )
        )
        return int(row["changes"])

    def unmapped_count(self) -> int:
        """Count native applications without a current core semantic section."""
        row = next(
            self._connection.execute(
                """
                SELECT COUNT(*) AS count FROM applications
                LEFT JOIN section_current
                    ON section_current.application_id = applications.id
                    AND section_current.section = 'application'
                WHERE section_current.application_id IS NULL
                """
            )
        )
        return int(row["count"])

    def database_integrity(self) -> str:
        """Run SQLite's local integrity check."""
        return str(next(self._connection.execute("PRAGMA integrity_check"))[0])

    def missing_evidence_paths(self) -> tuple[str, ...]:
        """Return retained evidence paths that no longer exist."""
        return tuple(
            row["path"]
            for row in self._connection.execute(
                "SELECT path FROM evidence ORDER BY path"
            )
            if not (self.evidence_root / row["path"]).exists()
        )

    def invalid_evidence_paths(self) -> tuple[str, ...]:
        """Return retained evidence paths that fail body-integrity verification."""
        invalid = []
        for row in self._connection.execute(
            """
            SELECT digest, path, source_url, media_type
            FROM evidence ORDER BY path
            """
        ):
            try:
                self._evidence.read_capture(
                    EvidenceDigest(row["digest"]),
                    row["path"],
                    row["source_url"],
                    row["media_type"],
                )
            except EvidenceIntegrityError:
                invalid.append(row["path"])
        return tuple(invalid)

    def retained_evidence(self) -> tuple[EvidenceCapture, ...]:
        """Rehydrate every retained evidence row in digest order."""
        return tuple(
            self._evidence.read_capture(
                EvidenceDigest(row["digest"]),
                row["path"],
                row["source_url"],
                row["media_type"],
            )
            for row in self._connection.execute(
                "SELECT digest, path, source_url, media_type "
                "FROM evidence ORDER BY digest"
            )
        )
    def evidence_registration_audit(
        self, authority_id: AuthorityId
    ) -> EvidenceRegistrationAudit:
        """Reconcile authority applications, native links, and evidence rows."""
        registrations: list[RetainedEvidenceRegistration] = []
        missing: list[EvidenceDigest] = []
        applications_with_evidence = 0
        rows = tuple(
            self._connection.execute(
                """
                SELECT application.id, rebuild.evidence_digests_json
                FROM applications AS application
                LEFT JOIN native_rebuild_inputs AS rebuild
                    ON rebuild.application_id = application.id
                WHERE application.authority_id = ?
                ORDER BY application.id
                """,
                (authority_id,),
            )
        )
        for row in rows:
            digest_values = (
                ()
                if row["evidence_digests_json"] is None
                else tuple(json.loads(row["evidence_digests_json"]))
            )
            if digest_values:
                applications_with_evidence += 1
            for digest_value in digest_values:
                digest = EvidenceDigest(digest_value)
                evidence = self._connection.execute(
                    "SELECT path FROM evidence WHERE digest = ?",
                    (digest,),
                ).fetchone()
                if evidence is None:
                    missing.append(digest)
                    continue
                registrations.append(
                    RetainedEvidenceRegistration(
                        application_id=ApplicationId(row["id"]),
                        digest=digest,
                        path=evidence["path"],
                    )
                )
        return EvidenceRegistrationAudit(
            application_count=len(rows),
            applications_with_evidence=applications_with_evidence,
            registrations=tuple(registrations),
            missing_digests=tuple(missing),
        )

    def migration_versions(self) -> tuple[int, ...]:
        """Return applied migration versions in order."""
        return tuple(
            row["version"]
            for row in self._connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )

    def storage_bytes(self) -> int:
        """Measure database and retained-evidence bytes."""
        candidates = (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        )
        database_bytes = sum(
            path.stat().st_size for path in candidates if path.exists()
        )
        evidence_bytes = sum(
            path.stat().st_size
            for path in self.evidence_root.rglob("*")
            if path.is_file()
        )
        return database_bytes + evidence_bytes

    def backup_database(self, target: Path) -> None:
        """Create a transactionally consistent SQLite snapshot."""
        with closing(sqlite3.connect(target)) as destination:
            self._connection.backup(destination)

    def _migrate(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = {
            row["version"]
            for row in self._connection.execute("SELECT version FROM schema_migrations")
        }
        resources = sorted(
            files("yimby.migrations").iterdir(), key=lambda item: item.name
        )
        for resource in resources:
            prefix = resource.name.split("_", maxsplit=1)[0]
            if not resource.name.endswith(".sql") or not prefix.isdigit():
                continue
            version = int(prefix)
            if version in applied:
                continue
            self._connection.executescript(resource.read_text())
            self._connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (version, resource.name, datetime.now(UTC).isoformat()),
            )
            self._connection.commit()

    def _application_id(self, normalised: NormalisedObservation) -> ApplicationId:
        return ApplicationId(
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

    def _commit_normalised(
        self,
        application_id: ApplicationId,
        normalised: NormalisedObservation,
    ) -> None:
        normalised = self._canonical_observation(normalised)
        application = _ApplicationSection(
            proposal=normalised.proposal,
            status=normalised.status,
        )
        application_payload: dict[str, object] = application.model_dump(mode="json")
        application_payload["metadata"] = normalised.metadata.model_dump(mode="json")
        sections = (
            (
                "application",
                json.dumps(application_payload, separators=(",", ":")),
                "complete",
            ),
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
        for section, payload, state in sections:
            if state in {"complete", "empty"}:
                self._commit_section(
                    application_id,
                    section,
                    payload,
                    normalised.normaliser_version,
                )
        self._commit_metadata(application_id, normalised)

    @staticmethod
    def _canonical_observation(
        normalised: NormalisedObservation,
    ) -> NormalisedObservation:
        metadata = normalised.metadata
        canonical_metadata = metadata.model_copy(
            update={
                "aliases": tuple(sorted(metadata.aliases)),
                "published_parties": tuple(sorted(metadata.published_parties)),
                "constraints": tuple(sorted(metadata.constraints)),
                "conditions": tuple(sorted(metadata.conditions)),
                "consultations": tuple(sorted(metadata.consultations)),
                "events": tuple(
                    sorted(
                        metadata.events,
                        key=lambda event: (
                            event.event_at,
                            event.event_type,
                            event.details or "",
                        ),
                    )
                ),
                "relationships": tuple(
                    sorted(
                        metadata.relationships,
                        key=lambda relationship: (
                            relationship.relationship_type,
                            relationship.related_reference,
                        ),
                    )
                ),
            }
        )
        return normalised.model_copy(
            update={
                "documents": tuple(
                    sorted(
                        normalised.documents,
                        key=lambda document: (document.title, str(document.url)),
                    )
                ),
                "comments": tuple(
                    sorted(
                        normalised.comments,
                        key=lambda comment: (comment.comment_id, comment.text),
                    )
                ),
                "metadata": canonical_metadata,
            }
        )

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

    def _record_observed_versions(
        self,
        observation_id: int,
        application_id: ApplicationId,
        native_hash: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO observation_native_versions(
                observation_id, application_id, payload_hash
            ) VALUES (?, ?, ?)
            """,
            (observation_id, application_id, native_hash),
        )
        self._connection.execute(
            """
            INSERT INTO observation_section_versions(
                observation_id, section, version_id
            )
            SELECT ?, section, version_id
            FROM section_current
            WHERE application_id = ?
            """,
            (observation_id, application_id),
        )

    def _commit_metadata(
        self,
        application_id: ApplicationId,
        normalised: NormalisedObservation,
    ) -> None:
        metadata = normalised.metadata
        previous = self._connection.execute(
            """
            SELECT comment_count, source_url FROM application_details
            WHERE application_id = ?
            """,
            (application_id,),
        ).fetchone()
        comments_complete = normalised.completeness.comments.kind in {
            "complete",
            "empty",
        }
        comment_count = (
            len(normalised.comments)
            if comments_complete or previous is None
            else previous["comment_count"]
        )
        source_url = (
            str(metadata.source_url)
            if metadata.source_url is not None
            else None
            if previous is None
            else previous["source_url"]
        )
        self._connection.execute(
            """
            INSERT INTO application_details(
                application_id, application_type, decision, address,
                received_date, validated_date, decision_date, comment_count,
                source_url, parties_json, officer_name, constraints_json,
                conditions_json, consultations_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(application_id) DO UPDATE SET
                application_type = excluded.application_type,
                decision = excluded.decision,
                address = excluded.address,
                received_date = excluded.received_date,
                validated_date = excluded.validated_date,
                decision_date = excluded.decision_date,
                comment_count = excluded.comment_count,
                source_url = COALESCE(
                    excluded.source_url, application_details.source_url
                ),
                parties_json = excluded.parties_json,
                officer_name = excluded.officer_name,
                constraints_json = excluded.constraints_json,
                conditions_json = excluded.conditions_json,
                consultations_json = excluded.consultations_json
            """,
            (
                application_id,
                metadata.application_type,
                metadata.decision,
                metadata.address,
                self._date_value(metadata.received_date),
                self._date_value(metadata.validated_date),
                self._date_value(metadata.decision_date),
                comment_count,
                source_url,
                json.dumps(metadata.published_parties, separators=(",", ":")),
                metadata.officer_name,
                json.dumps(metadata.constraints, separators=(",", ":")),
                json.dumps(metadata.conditions, separators=(",", ":")),
                json.dumps(metadata.consultations, separators=(",", ":")),
            ),
        )
        self._replace_location(application_id, metadata.location)
        self._replace_aliases(application_id, normalised)
        self._replace_dates(application_id, metadata)
        if normalised.completeness.documents.kind in {"complete", "empty"}:
            self._replace_documents(application_id, normalised.documents)
        if comments_complete:
            self._replace_comments(application_id, normalised.comments)
        self._insert_events(application_id, metadata)
        self._replace_relationships(application_id, metadata)

    def _replace_location(
        self,
        application_id: ApplicationId,
        location: ApplicationLocation | None,
    ) -> None:
        self._connection.execute(
            "DELETE FROM application_locations WHERE application_id = ?",
            (application_id,),
        )
        if location is not None:
            self._connection.execute(
                """
                INSERT INTO application_locations(
                    application_id, bng_easting, bng_northing, longitude, latitude
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    location.bng_easting,
                    location.bng_northing,
                    location.wgs84.longitude,
                    location.wgs84.latitude,
                ),
            )

    def _replace_aliases(
        self,
        application_id: ApplicationId,
        normalised: NormalisedObservation,
    ) -> None:
        self._connection.execute(
            "DELETE FROM application_aliases WHERE application_id = ?",
            (application_id,),
        )
        for alias in normalised.metadata.aliases:
            self._connection.execute(
                """
                INSERT INTO application_aliases(application_id, alias, source_id)
                VALUES (?, ?, ?)
                """,
                (application_id, alias, normalised.reference.source_id),
            )

    def _replace_dates(
        self,
        application_id: ApplicationId,
        metadata: ApplicationMetadata,
    ) -> None:
        self._connection.execute(
            "DELETE FROM application_dates WHERE application_id = ?",
            (application_id,),
        )
        dates = (
            ("received", metadata.received_date),
            ("validated", metadata.validated_date),
            ("decision", metadata.decision_date),
        )
        for date_kind, value in dates:
            if value is not None:
                self._connection.execute(
                    """
                    INSERT INTO application_dates(
                        application_id, date_kind, date_value
                    ) VALUES (?, ?, ?)
                    """,
                    (application_id, date_kind, value.isoformat()),
                )

    def _replace_documents(
        self,
        application_id: ApplicationId,
        documents: tuple[DocumentRecord, ...],
    ) -> None:
        self._connection.execute(
            "DELETE FROM document_metadata WHERE application_id = ?",
            (application_id,),
        )
        for document in documents:
            key = sha256(f"{document.title}\0{document.url}".encode()).hexdigest()
            self._connection.execute(
                """
                INSERT INTO document_metadata(
                    application_id, document_key, title, url
                ) VALUES (?, ?, ?, ?)
                """,
                (application_id, key, document.title, str(document.url)),
            )

    def _replace_comments(
        self,
        application_id: ApplicationId,
        comments: tuple[CommentRecord, ...],
    ) -> None:
        self._connection.execute(
            "DELETE FROM comment_records WHERE application_id = ?",
            (application_id,),
        )
        for comment in comments:
            self._connection.execute(
                """
                INSERT INTO comment_records(application_id, comment_id, text)
                VALUES (?, ?, ?)
                """,
                (application_id, comment.comment_id, comment.text),
            )

    def _insert_events(
        self,
        application_id: ApplicationId,
        metadata: ApplicationMetadata,
    ) -> None:
        for event in metadata.events:
            digest = sha256(event.model_dump_json().encode()).hexdigest()
            self._connection.execute(
                """
                INSERT OR IGNORE INTO application_events(
                    application_id, event_type, event_at, details, semantic_hash
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    event.event_type,
                    event.event_at.isoformat(),
                    event.details,
                    digest,
                ),
            )

    def _replace_relationships(
        self,
        application_id: ApplicationId,
        metadata: ApplicationMetadata,
    ) -> None:
        self._connection.execute(
            "DELETE FROM application_relationships WHERE application_id = ?",
            (application_id,),
        )
        for relationship in metadata.relationships:
            self._connection.execute(
                """
                INSERT INTO application_relationships(
                    application_id, related_reference, relationship_type
                ) VALUES (?, ?, ?)
                """,
                (
                    application_id,
                    relationship.related_reference,
                    relationship.relationship_type,
                ),
            )

    def _set_refresh_schedule(
        self,
        application_id: ApplicationId,
        normalised: NormalisedObservation,
        observed_at: datetime,
    ) -> None:
        decision_date = normalised.metadata.decision_date
        within_decision_window = (
            decision_date is not None
            and observed_at.date() <= decision_date + timedelta(days=90)
        )
        if decision_date is not None and not within_decision_window:
            cadence = "quarterly"
            reason = "decided-over-90-days"
            delay = timedelta(days=90)
        else:
            cadence = "weekly"
            reason = (
                "decided-within-90-days"
                if within_decision_window
                else "active-or-recent"
            )
            delay = timedelta(days=7)
        self._connection.execute(
            """
            INSERT INTO refresh_schedules(
                application_id, next_refresh_at, cadence, reason
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(application_id) DO UPDATE SET
                next_refresh_at = excluded.next_refresh_at,
                cadence = excluded.cadence,
                reason = excluded.reason
            """,
            (
                application_id,
                (observed_at + delay).isoformat(),
                cadence,
                reason,
            ),
        )

    def _update_observed_capabilities(
        self,
        normalised: NormalisedObservation,
    ) -> None:
        row = self._connection.execute(
            """
            SELECT capabilities_json FROM authorities WHERE authority_id = ?
            """,
            (normalised.authority_id,),
        ).fetchone()
        if row is None:
            return
        capabilities = AuthorityCapabilities.model_validate_json(
            row["capabilities_json"]
        )
        updated = capabilities.model_copy(
            update={
                "documents": self._capability_state(
                    normalised.completeness.documents.kind
                ),
                "comments": self._capability_state(
                    normalised.completeness.comments.kind
                ),
                "coordinates": (
                    CapabilityState.SUPPORTED
                    if normalised.metadata.location is not None
                    else capabilities.coordinates
                ),
            }
        )
        self._connection.execute(
            """
            UPDATE authorities SET capabilities_json = ? WHERE authority_id = ?
            """,
            (updated.model_dump_json(), normalised.authority_id),
        )

    def _metadata_from_rows(
        self,
        detail: sqlite3.Row | None,
        location_row: sqlite3.Row | None,
        aliases: tuple[str, ...],
        events: tuple[ApplicationEvent, ...],
        relationships: tuple[ApplicationRelationship, ...],
    ) -> ApplicationMetadata:
        if detail is None:
            return ApplicationMetadata(
                aliases=aliases,
                location=self._location_from_row(location_row),
                events=events,
                relationships=relationships,
            )
        return ApplicationMetadata(
            aliases=aliases,
            application_type=detail["application_type"],
            decision=detail["decision"],
            address=detail["address"],
            received_date=self._optional_date(detail["received_date"]),
            validated_date=self._optional_date(detail["validated_date"]),
            decision_date=self._optional_date(detail["decision_date"]),
            location=self._location_from_row(location_row),
            source_url=(
                None if detail["source_url"] is None else HttpUrl(detail["source_url"])
            ),
            published_parties=tuple(json.loads(detail["parties_json"])),
            officer_name=detail["officer_name"],
            constraints=tuple(json.loads(detail["constraints_json"])),
            conditions=tuple(json.loads(detail["conditions_json"])),
            consultations=tuple(json.loads(detail["consultations_json"])),
            events=events,
            relationships=relationships,
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> ApplicationEvent:
        return ApplicationEvent(
            event_type=row["event_type"],
            event_at=datetime.fromisoformat(row["event_at"]),
            details=row["details"],
        )

    @staticmethod
    def _location_from_row(row: sqlite3.Row | None) -> ApplicationLocation | None:
        if row is None:
            return None
        return ApplicationLocation(
            bng_easting=row["bng_easting"],
            bng_northing=row["bng_northing"],
            wgs84=Wgs84Coordinate(
                longitude=row["longitude"],
                latitude=row["latitude"],
            ),
        )

    @staticmethod
    def _date_value(value: date | None) -> str | None:
        return None if value is None else value.isoformat()

    @staticmethod
    def _optional_date(value: str | None) -> date | None:
        return None if value is None else date.fromisoformat(value)

    @staticmethod
    def _capability_state(kind: str) -> CapabilityState:
        if kind in {"complete", "empty"}:
            return CapabilityState.SUPPORTED
        if kind == "unavailable":
            return CapabilityState.UNSUPPORTED
        return CapabilityState.UNKNOWN

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
