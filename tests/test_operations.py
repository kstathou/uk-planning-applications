# Copyright (c) 2026 Kostas Stathoulopoulos

"""Behaviour tests for the offline operational product surface."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from pydantic import HttpUrl

from yimby import (
    AuthorityId,
    Collector,
    DiscoveryWindow,
    barnet_registry,
    pilot_registry,
)
from yimby.adapters import NativeSchemaMismatchError
from yimby.authorities.barnet import BARNET_PACKAGE
from yimby.authorities.barnet.fixtures import fixture_session
from yimby.authorities.camden.fixtures import fixture_session as camden_fixtures
from yimby.backup import (
    BackupTargetExistsError,
    BackupVerificationError,
    RestoreTargetExistsError,
    create_backup,
    default_backup_path,
    restore_backup,
    verify_backup,
)
from yimby.dashboard import dashboard_snapshot, search_dashboard
from yimby.doctor import run_doctor
from yimby.domain import (
    ApplicationEvent,
    ApplicationId,
    ApplicationMetadata,
    ApplicationRelationship,
    CapabilityState,
    CollectedObservation,
    CommentRecord,
    Completeness,
    CompleteSection,
    DocumentRecord,
    EvidenceCapture,
    EvidenceDigest,
    NormalisedObservation,
    Provenance,
    RunMetrics,
    RunOutcome,
    RunStatus,
    SourceId,
    SourceReference,
    TransportMode,
)
from yimby.evidence import EvidenceStore
from yimby.exporting import (
    PUBLIC_ALLOWLIST,
    ExportFormat,
    ExportProfile,
    export_records,
)
from yimby.geo import bng_to_wgs84
from yimby.normalise import rebuild_normalised
from yimby.store import SqliteStore
from yimby.transport import (
    FixtureResponse,
    FixtureSession,
    PortalRequest,
    RequestIntent,
    SourceUnavailableError,
)

WINDOW = DiscoveryWindow(
    start=date(2026, 8, 16),
    end=date(2026, 9, 15),
)
CAMDEN_FIXTURE_EASTING = 530748
PILOT_AUTHORITY_COUNT = 15
UNCHANGED_AND_REBUILT_VERSIONS = 2
BARNET_REQUEST_COUNT = 3
REPEATED_RETRY_ATTEMPTS = 2
INTERRUPTED_RETRY_ATTEMPTS = 3
REMOVAL_AND_REVERSION_CHANGES = 2


def _store(root: Path) -> SqliteStore:
    return SqliteStore(
        root / "yimby.sqlite3",
        EvidenceStore(root / "evidence"),
    )


def _collect_barnet(store: SqliteStore) -> ApplicationId:
    report = asyncio.run(
        Collector(barnet_registry(), store).collect(
            AuthorityId("barnet"),
            WINDOW,
            fixture_session(WINDOW),
        )
    )
    return report.applications[0]


def _rich_observation() -> CollectedObservation:
    body = b'{"native":"retained"}'
    digest = EvidenceDigest(sha256(body).hexdigest())
    reference = SourceReference(
        source_id=SourceId("barnet-idox-current"),
        reference="RICH/2026/1",
    )
    location = bng_to_wgs84(530000, 180000)
    assert location is not None
    normalised = NormalisedObservation(
        authority_id=AuthorityId("barnet"),
        reference=reference,
        proposal="Rich operational record",
        status="decided",
        documents=(
            DocumentRecord(
                title="Decision notice",
                url=HttpUrl("https://example.test/decision.pdf"),
            ),
        ),
        comments=(CommentRecord(comment_id="comment-1", text="Support"),),
        completeness=Completeness(
            application=CompleteSection(item_count=1),
            documents=CompleteSection(item_count=1),
            comments=CompleteSection(item_count=1),
        ),
        provenance=(Provenance(field="proposal", evidence=digest),),
        normaliser_version="rich-v1",
        metadata=ApplicationMetadata(
            aliases=("ALIAS-1",),
            application_type="Full",
            decision="Granted",
            address="1 Test Street",
            received_date=date(2026, 8, 1),
            validated_date=date(2026, 8, 2),
            decision_date=date(2026, 9, 1),
            location=location,
            source_url=HttpUrl("https://example.test/application/RICH-2026-1"),
            published_parties=("Applicant Person",),
            officer_name="Officer Person",
            constraints=("Conservation area",),
            conditions=("Materials",),
            consultations=("Parish council",),
            events=(
                ApplicationEvent(
                    event_type="decision",
                    event_at=datetime(2026, 9, 1, tzinfo=UTC),
                    details="Granted",
                ),
            ),
            relationships=(
                ApplicationRelationship(
                    related_reference="RELATED/1",
                    relationship_type="supersedes",
                ),
            ),
        ),
    )
    return CollectedObservation(
        native_schema="RichApplicationV1",
        native_json=body.decode(),
        normalised=normalised,
        evidence=(
            EvidenceCapture(
                url=HttpUrl("https://example.test/application/RICH-2026-1"),
                media_type="application/json",
                body=body,
                digest=digest,
            ),
        ),
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def _commit_rich(store: SqliteStore) -> ApplicationId:
    store.register_authorities(barnet_registry().manifests())
    run_id = store.begin_run(AuthorityId("barnet"))
    application_id = store.commit_observation(run_id, _rich_observation())
    store.finish_run(
        run_id,
        AuthorityId("barnet"),
        RunOutcome(
            status=RunStatus.SUCCEEDED,
            metrics=RunMetrics(
                request_count=1,
                transferred_bytes=21,
                duration_ms=3,
                storage_growth_bytes=5,
            ),
            transport_mode=TransportMode.FIXTURE,
        ),
    )
    return application_id


def test_rich_storage_location_search_and_operational_state(tmp_path: Path) -> None:
    """Rich fields, schedules, metrics, search, and corrections remain typed."""
    store = _store(tmp_path)
    application_id = _commit_rich(store)

    view = store.application_view(application_id)
    assert view.metadata.model_dump(mode="json") == {
        "aliases": ["ALIAS-1"],
        "application_type": "Full",
        "decision": "Granted",
        "address": "1 Test Street",
        "received_date": "2026-08-01",
        "validated_date": "2026-08-02",
        "decision_date": "2026-09-01",
        "location": {
            "bng_easting": 530000.0,
            "bng_northing": 180000.0,
            "wgs84": {
                "longitude": pytest.approx(-0.12835394),
                "latitude": pytest.approx(51.50399083),
            },
        },
        "source_url": "https://example.test/application/RICH-2026-1",
        "published_parties": ["Applicant Person"],
        "officer_name": "Officer Person",
        "constraints": ["Conservation area"],
        "conditions": ["Materials"],
        "consultations": ["Parish council"],
        "events": [
            {
                "event_type": "decision",
                "event_at": "2026-09-01T00:00:00Z",
                "details": "Granted",
            }
        ],
        "relationships": [
            {
                "related_reference": "RELATED/1",
                "relationship_type": "supersedes",
            }
        ],
    }
    assert store.search_applications("test street") == (view,)
    assert store.search_applications("missing") == ()
    assert store.metrics_totals().model_dump() == {
        "request_count": 1,
        "transferred_bytes": 21,
        "duration_ms": 3,
        "browser_time_ms": 0,
        "storage_growth_bytes": 5,
    }
    states = store.authority_states(datetime(2020, 1, 1, tzinfo=UTC))
    assert states[0].freshness_days == 0
    assert states[0].manifest.capabilities.documents == CapabilityState.SUPPORTED
    assert states[0].manifest.capabilities.comments == CapabilityState.SUPPORTED
    assert states[0].manifest.capabilities.coordinates == CapabilityState.SUPPORTED
    assert store.application_count() == 1
    assert store.observed_change_count() == 0
    assert store.unmapped_count() == 0
    assert store.database_integrity() == "ok"
    assert store.storage_bytes() > 0

    rich_reference = _rich_observation().normalised.reference
    assert (
        store.collection_work(
            AuthorityId("barnet"),
            datetime(2026, 9, 7, 23, 59, tzinfo=UTC),
        )
        == ()
    )
    assert store.collection_work(
        AuthorityId("barnet"),
        datetime(2026, 9, 8, tzinfo=UTC),
    ) == (rich_reference,)

    store.set_refresh_schedule(
        application_id,
        datetime(2026, 10, 1, tzinfo=UTC),
        "quarterly",
        "decided",
    )
    assert (
        store.collection_work(
            AuthorityId("barnet"),
            datetime(2026, 9, 30, tzinfo=UTC),
        )
        == ()
    )
    assert store.collection_work(
        AuthorityId("barnet"),
        datetime(2026, 10, 1, tzinfo=UTC),
    ) == (rich_reference,)
    store.set_suppression(application_id, suppressed=True, reason="reviewed")
    assert store.application_views() == ()
    store.set_suppression(application_id, suppressed=False, reason="corrected")
    assert store.application_views() == (store.application_view(application_id),)
    with pytest.raises(KeyError):
        store.set_suppression(
            ApplicationId("missing"),
            suppressed=True,
            reason="invalid",
        )
    store.close()


def test_old_decisions_receive_a_quarterly_refresh_schedule(tmp_path: Path) -> None:
    """A decision older than ninety days is no longer placed on weekly refresh."""
    store = _store(tmp_path)
    store.register_authorities(barnet_registry().manifests())
    original = _rich_observation()
    reference = SourceReference(
        source_id=SourceId("barnet-idox-current"),
        reference="OLD/2026/1",
    )
    normalised = original.normalised.model_copy(
        update={
            "reference": reference,
            "metadata": original.normalised.metadata.model_copy(
                update={"decision_date": date(2026, 1, 1)}
            ),
        }
    )
    observed_at = datetime(2026, 9, 1, tzinfo=UTC)
    old = original.model_copy(
        update={"normalised": normalised, "observed_at": observed_at}
    )
    run_id = store.begin_run(AuthorityId("barnet"))
    store.commit_observation(run_id, old)

    assert (
        store.collection_work(
            AuthorityId("barnet"),
            observed_at + timedelta(days=89),
        )
        == ()
    )
    assert store.collection_work(
        AuthorityId("barnet"),
        observed_at + timedelta(days=90),
    ) == (reference,)
    store.close()


def test_semantic_ordering_does_not_create_false_changes(tmp_path: Path) -> None:
    """Portal row reordering is canonical while native payload order is retained."""
    store = _store(tmp_path)
    store.register_authorities(barnet_registry().manifests())
    original = _rich_observation()
    second_document = DocumentRecord(
        title="Application form",
        url=HttpUrl("https://example.test/application.pdf"),
    )
    second_comment = CommentRecord(comment_id="comment-2", text="Object")
    first_normalised = original.normalised.model_copy(
        update={
            "documents": (*original.normalised.documents, second_document),
            "comments": (*original.normalised.comments, second_comment),
            "metadata": original.normalised.metadata.model_copy(
                update={
                    "aliases": ("Z-ALIAS", "A-ALIAS"),
                    "constraints": ("Trees", "Flooding"),
                }
            ),
        }
    )
    second_normalised = first_normalised.model_copy(
        update={
            "documents": tuple(reversed(first_normalised.documents)),
            "comments": tuple(reversed(first_normalised.comments)),
            "metadata": first_normalised.metadata.model_copy(
                update={
                    "aliases": tuple(reversed(first_normalised.metadata.aliases)),
                    "constraints": tuple(
                        reversed(first_normalised.metadata.constraints)
                    ),
                }
            ),
        }
    )
    for normalised in (first_normalised, second_normalised):
        run_id = store.begin_run(AuthorityId("barnet"))
        store.commit_observation(
            run_id,
            original.model_copy(update={"normalised": normalised}),
        )

    application_id = store.application_views()[0].application.id
    assert store.semantic_version_count(application_id, "application") == 1
    assert store.semantic_version_count(application_id, "documents") == 1
    assert store.semantic_version_count(application_id, "comments") == 1
    view = store.application_view(application_id)
    assert view.metadata.aliases == ("A-ALIAS", "Z-ALIAS")
    assert view.metadata.constraints == ("Flooding", "Trees")
    assert [document.title for document in view.application.documents] == [
        "Application form",
        "Decision notice",
    ]
    store.close()


def test_removal_and_reversion_preserve_observed_transition_order(
    tmp_path: Path,
) -> None:
    """A to B to A remains two observed changes with two reusable states."""
    store = _store(tmp_path)
    store.register_authorities(barnet_registry().manifests())
    original = _rich_observation()
    added = DocumentRecord(
        title="Application form",
        url=HttpUrl("https://example.test/application.pdf"),
    )
    states = (
        ("with-form", (*original.normalised.documents, added)),
        ("removed-form", original.normalised.documents),
        ("restored-form", (*original.normalised.documents, added)),
    )
    application_id: ApplicationId | None = None
    for native_state, documents in states:
        run_id = store.begin_run(AuthorityId("barnet"))
        application_id = store.commit_observation(
            run_id,
            original.model_copy(
                update={
                    "native_json": json.dumps({"state": native_state}),
                    "normalised": original.normalised.model_copy(
                        update={"documents": documents}
                    ),
                }
            ),
        )

    assert application_id is not None
    assert (
        store.semantic_version_count(application_id, "documents")
        == REMOVAL_AND_REVERSION_CHANGES
    )
    assert store.observed_change_count() == REMOVAL_AND_REVERSION_CHANGES
    assert [item.title for item in store.get_application(application_id).documents] == [
        "Application form",
        "Decision notice",
    ]
    store.close()


def test_normaliser_version_change_is_not_a_source_change(tmp_path: Path) -> None:
    """Identical source semantics stay unchanged across a normaliser release."""
    store = _store(tmp_path)
    store.register_authorities(barnet_registry().manifests())
    original = _rich_observation()
    for version in ("rich-v1", "rich-v2"):
        run_id = store.begin_run(AuthorityId("barnet"))
        store.commit_observation(
            run_id,
            original.model_copy(
                update={
                    "normalised": original.normalised.model_copy(
                        update={"normaliser_version": version}
                    )
                }
            ),
        )

    application_id = store.application_views()[0].application.id
    assert (
        store.semantic_version_count(application_id, "application")
        == REMOVAL_AND_REVERSION_CHANGES
    )
    assert store.observed_change_count() == 0
    store.close()


def test_coordinate_conversion_and_camden_fixture(tmp_path: Path) -> None:
    """Complete BNG pairs convert to WGS84 while missing pairs stay unknown."""
    assert bng_to_wgs84(None, 180000) is None
    assert bng_to_wgs84(530000, None) is None
    location = bng_to_wgs84(530000, 180000)
    assert location is not None
    assert location.wgs84.longitude == pytest.approx(-0.12835394)
    assert location.wgs84.latitude == pytest.approx(51.50399083)

    store = _store(tmp_path)
    report = asyncio.run(
        Collector(pilot_registry(), store).collect(
            AuthorityId("camden"),
            WINDOW,
            camden_fixtures(WINDOW),
        )
    )
    camden = store.application_view(report.applications[0])
    assert camden.metadata.location is not None
    assert camden.metadata.location.bng_easting == CAMDEN_FIXTURE_EASTING
    store.close()


def test_offline_rebuild_preserves_versions_and_requires_matching_schema(
    tmp_path: Path,
) -> None:
    """Rebuilds make zero requests and distinguish normaliser versions."""
    store = _store(tmp_path)
    application_id = _collect_barnet(store)
    retained = store.retained_native_records()[0]
    rebuilt_v2 = BARNET_PACKAGE.rebuild(retained).model_copy(
        update={"normaliser_version": "barnet-v3"}
    )
    store.commit_rebuild(application_id, rebuilt_v2)
    assert (
        store.semantic_version_count(application_id, "application")
        == UNCHANGED_AND_REBUILT_VERSIONS
    )
    assert (
        store.semantic_version_count(application_id, "documents")
        == UNCHANGED_AND_REBUILT_VERSIONS
    )
    assert (
        store.semantic_version_count(application_id, "comments")
        == UNCHANGED_AND_REBUILT_VERSIONS
    )

    report = rebuild_normalised(store, barnet_registry())
    assert report.model_dump() == {"rebuilt": 1, "transport_requests": 0}
    assert store.application_view(application_id).normaliser_version == "barnet-v2"
    assert store.observed_change_count() == 0
    assert (
        store.semantic_version_count(application_id, "application")
        == UNCHANGED_AND_REBUILT_VERSIONS
    )

    mismatched = retained.model_copy(update={"native_schema": "WrongV1"})
    with pytest.raises(NativeSchemaMismatchError, match="expected native schema"):
        BARNET_PACKAGE.rebuild(mismatched)
    store.close()


def test_retained_native_requires_registered_evidence(tmp_path: Path) -> None:
    """A corrupt native-to-evidence link fails instead of fabricating provenance."""
    store = _store(tmp_path)
    _collect_barnet(store)
    store.close()
    with closing(sqlite3.connect(tmp_path / "yimby.sqlite3")) as connection:
        connection.execute("DELETE FROM evidence")
        connection.commit()
    reopened = _store(tmp_path)
    with pytest.raises(KeyError):
        reopened.retained_native_records()
    reopened.close()


def test_exports_are_deterministic_profiled_and_suppressed(tmp_path: Path) -> None:
    """Public output is allowlisted and every format is deterministic/readable."""
    store = _store(tmp_path / "data")
    application_id = _commit_rich(store)
    first_public = tmp_path / "public-1.jsonl"
    second_public = tmp_path / "public-2.jsonl"
    research = tmp_path / "research.jsonl"
    csv_path = tmp_path / "research.csv"
    parquet_one = tmp_path / "research-1.parquet"
    parquet_two = tmp_path / "research-2.parquet"

    assert (
        export_records(store, first_public, ExportFormat.JSONL, ExportProfile.PUBLIC)
        == 1
    )
    assert (
        export_records(store, second_public, ExportFormat.JSONL, ExportProfile.PUBLIC)
        == 1
    )
    assert first_public.read_bytes() == second_public.read_bytes()
    public = json.loads(first_public.read_text())
    assert tuple(public) == tuple(sorted(PUBLIC_ALLOWLIST))
    assert "comments" not in public
    assert "published_parties" not in public
    assert "officer_name" not in public
    assert "native_json" not in public
    assert public["comment_count"] == 1
    assert public["source_url"] == "https://example.test/application/RICH-2026-1"
    assert public["source_reuse"].startswith("Verify the linked authority source")

    export_records(store, research, ExportFormat.JSONL, ExportProfile.RESEARCH)
    research_row = json.loads(research.read_text())
    assert research_row["comments"] == [{"comment_id": "comment-1", "text": "Support"}]
    assert research_row["published_parties"] == ["Applicant Person"]
    export_records(store, csv_path, ExportFormat.CSV, ExportProfile.RESEARCH)
    assert "Applicant Person" in csv_path.read_text()
    export_records(store, parquet_one, ExportFormat.PARQUET, ExportProfile.RESEARCH)
    export_records(store, parquet_two, ExportFormat.PARQUET, ExportProfile.RESEARCH)
    assert parquet_one.read_bytes() == parquet_two.read_bytes()
    table = pq.read_table(parquet_one)
    assert table.num_rows == 1
    assert table.column("reference")[0].as_py() == "RICH/2026/1"

    store.set_suppression(application_id, suppressed=True, reason="privacy")
    suppressed = tmp_path / "suppressed.jsonl"
    assert (
        export_records(store, suppressed, ExportFormat.JSONL, ExportProfile.PUBLIC) == 0
    )
    assert suppressed.read_text() == ""
    store.close()


def test_backup_restore_verification_and_no_overwrite(tmp_path: Path) -> None:
    """A consistent backup restores evidence and refuses destructive targets."""
    data = tmp_path / "data"
    store = _store(data)
    application_id = _collect_barnet(store)
    backup = create_backup(store, tmp_path / "backup")
    verify_backup(backup)
    assert default_backup_path(data).parent == data / "backups"
    with pytest.raises(BackupTargetExistsError):
        create_backup(store, backup)
    restored = restore_backup(backup, tmp_path / "restored")
    restored_store = _store(restored)
    assert restored_store.get_application(application_id).proposal == (
        "Build two homes & plant four trees"
    )
    assert restored_store.missing_evidence_paths() == ()
    restored_store.close()
    with pytest.raises(RestoreTargetExistsError):
        restore_backup(backup, restored)
    store.close()

    missing_manifest = tmp_path / "missing-manifest"
    missing_manifest.mkdir()
    with pytest.raises(BackupVerificationError, match="missing manifest"):
        verify_backup(missing_manifest)

    manifest_path = backup / "manifest.json"
    original_manifest = manifest_path.read_text()
    manifest = json.loads(original_manifest)
    manifest["schema_version"] = 999
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(BackupVerificationError, match="unsupported manifest"):
        verify_backup(backup)
    manifest_path.write_text(original_manifest)

    evidence_file = next((backup / "evidence").rglob("*.gz"))
    original_evidence = evidence_file.read_bytes()
    evidence_file.write_bytes(b"tampered")
    with pytest.raises(
        BackupVerificationError, match=str(evidence_file.relative_to(backup))
    ):
        verify_backup(backup)
    evidence_file.write_bytes(original_evidence)

    database = backup / "yimby.sqlite3"
    original_database = database.read_bytes()
    database.write_bytes(b"not sqlite")
    manifest = json.loads(original_manifest)
    manifest["files"]["yimby.sqlite3"] = {
        "sha256": sha256(b"not sqlite").hexdigest(),
        "size": len(b"not sqlite"),
    }
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(BackupVerificationError, match="database integrity"):
        verify_backup(backup)
    database.write_bytes(original_database)
    manifest_path.write_text(original_manifest)


def test_backup_empty_evidence_and_integrity_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backup verification reports empty and corrupt storage boundaries."""
    empty_store = _store(tmp_path / "empty-data")
    empty_backup = create_backup(empty_store, tmp_path / "empty-backup")
    assert (empty_backup / "evidence").is_dir()
    empty_store.close()

    store = _store(tmp_path / "data")
    _collect_barnet(store)
    backup = create_backup(store, tmp_path / "backup")
    store.close()

    broken_connection = MagicMock()
    broken_connection.execute.return_value.fetchone.return_value = ("broken",)
    with monkeypatch.context() as patch:
        patch.setattr(
            "yimby.backup.sqlite3.connect",
            MagicMock(return_value=broken_connection),
        )
        with pytest.raises(BackupVerificationError, match="database integrity"):
            verify_backup(backup)

    def copy_empty(_source: Path, destination: Path) -> Path:
        destination.mkdir()
        return destination

    with monkeypatch.context() as patch:
        patch.setattr("yimby.backup.shutil.copytree", copy_empty)
        with pytest.raises(BackupVerificationError, match="restored evidence"):
            restore_backup(backup, tmp_path / "missing-restored-evidence")


def test_unregistered_collection_and_legacy_metadata_are_explicit(
    tmp_path: Path,
) -> None:
    """Legacy applications without operational metadata remain inspectable."""
    store = _store(tmp_path)
    run_id = store.begin_run(AuthorityId("barnet"))
    application_id = store.commit_observation(run_id, _rich_observation())
    store.close()
    with closing(sqlite3.connect(tmp_path / "yimby.sqlite3")) as connection:
        connection.execute(
            "DELETE FROM application_details WHERE application_id = ?",
            (application_id,),
        )
        connection.commit()
    reopened = _store(tmp_path)
    legacy = reopened.application_view(application_id)
    assert legacy.metadata.application_type is None
    assert legacy.metadata.aliases == ("ALIAS-1",)
    reopened.close()


def test_doctor_dashboard_migrations_and_examples(tmp_path: Path) -> None:
    """Health and dashboard models expose complete 15-authority denominators."""
    store = _store(tmp_path / "data")
    application_id = _collect_barnet(store)
    assert store.migration_versions() == (1, 2, 3, 4, 5)
    healthy = run_doctor(
        store,
        tmp_path / "data",
        expected_authorities=1,
        minimum_free_bytes=0,
    )
    assert healthy.ok
    assert all(check.ok for check in healthy.checks)

    store.register_authorities(pilot_registry().manifests())
    dashboard = dashboard_snapshot(store, pilot_registry())
    assert dashboard.coverage_implemented == PILOT_AUTHORITY_COUNT
    assert dashboard.coverage_denominator == PILOT_AUTHORITY_COUNT
    assert dashboard.live_ready == 0
    assert dashboard.live_readiness_denominator == PILOT_AUTHORITY_COUNT
    assert dashboard.application_count == 1
    assert dashboard.request_count == BARNET_REQUEST_COUNT
    hits = search_dashboard(store, "two homes")
    assert hits[0].application_id == application_id
    assert search_dashboard(store, "not-present") == ()

    evidence_path = next((tmp_path / "data" / "evidence").rglob("*.gz"))
    evidence_path.unlink()
    unhealthy = run_doctor(
        store,
        tmp_path / "data",
        expected_authorities=99,
        minimum_free_bytes=10**30,
    )
    assert not unhealthy.ok
    failed_names = {check.name for check in unhealthy.checks if not check.ok}
    assert failed_names == {"evidence", "authority-registry", "disk-space"}
    store.close()

    reopened = _store(tmp_path / "data")
    assert reopened.migration_versions() == (1, 2, 3, 4, 5)
    reopened.close()

    launchd = Path("examples/launchd/com.example.yimby-sync.plist.example").read_text()
    service = Path("examples/systemd/yimby-sync.service.example").read_text()
    timer = Path("examples/systemd/yimby-sync.timer.example").read_text()
    assert "<key>Disabled</key>\n  <true/>" in launchd
    assert "/path/to/yimby" in launchd
    assert "ConditionPathExists=/path/to/enable-yimby-sync" in service
    assert "[Install]" not in timer
    assert "Persistent=false" in timer


class _CancellingSession:
    def __init__(self, search_url: str) -> None:
        self._inner = FixtureSession(
            {
                search_url: FixtureResponse(
                    body=(
                        b'<li data-reference="23/0001"></li>'
                        b'<span data-next-cursor="complete"></span>'
                    )
                )
            }
        )

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        if request.intent == RequestIntent.DETAIL:
            raise asyncio.CancelledError
        return await self._inner.fetch(request)

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return self._inner.requested_urls

    @property
    def attachment_body_requests(self) -> int:
        return self._inner.attachment_body_requests

    @property
    def transferred_bytes(self) -> int:
        return self._inner.transferred_bytes

    @property
    def browser_time_ms(self) -> int:
        return 0

    @property
    def mode(self) -> TransportMode:
        return TransportMode.FIXTURE

    async def aclose(self) -> None:
        await self._inner.aclose()


def test_failed_interrupted_and_retried_detail_state(tmp_path: Path) -> None:
    """Failed and interrupted detail work stays retryable until success."""
    store = _store(tmp_path)
    collector = Collector(barnet_registry(), store)
    valid = fixture_session(WINDOW)
    search_only = FixtureSession(
        {
            url: FixtureResponse(
                body=(
                    b'<li data-reference="23/0001"></li>'
                    b'<span data-next-cursor="complete"></span>'
                )
            )
            for url in valid.available_urls[:2]
        }
    )
    for _ in range(2):
        with pytest.raises(SourceUnavailableError, match="application/23/0001"):
            asyncio.run(collector.collect(AuthorityId("barnet"), WINDOW, search_only))
    pending = store.retry_items()[0]
    assert pending.attempts == REPEATED_RETRY_ATTEMPTS
    assert pending.status == "pending"
    assert store.run_statuses() == (RunStatus.FAILED, RunStatus.FAILED)

    successful = fixture_session(WINDOW)
    report = asyncio.run(
        collector.collect(AuthorityId("barnet"), WINDOW, fixture_session(WINDOW))
    )
    assert report.requested_urls == (
        successful.available_urls[2],
        successful.available_urls[3],
        successful.available_urls[1],
    )
    assert store.retry_items()[0].status == "succeeded"
    assert store.run_statuses()[-1] == RunStatus.SUCCEEDED

    cancelling = _CancellingSession(valid.available_urls[1])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(collector.collect(AuthorityId("barnet"), WINDOW, cancelling))
    assert store.run_statuses()[-1] == RunStatus.INTERRUPTED
    assert store.retry_items()[0].attempts == INTERRUPTED_RETRY_ATTEMPTS
    assert (
        store.prune_expired_diagnostics(datetime.now(UTC) + timedelta(days=31))
        == INTERRUPTED_RETRY_ATTEMPTS
    )
    assert store.prune_expired_diagnostics(datetime.now(UTC)) == 0
    store.close()
