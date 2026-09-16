# Copyright (c) 2026 Kostas Stathoulopoulos

"""Behaviour tests for the first collection slice."""

from __future__ import annotations

import asyncio
import gzip
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import HttpUrl, ValidationError

from yimby import (
    AuthorityId,
    AuthorityRegistry,
    Collector,
    DiscoveryWindow,
    barnet_registry,
)
from yimby.authorities.barnet import BARNET_PACKAGE
from yimby.authorities.barnet.adapter import BarnetParseError
from yimby.authorities.barnet.fixtures import fixture_session
from yimby.domain import (
    ApplicationId,
    AuthorityKind,
    AuthorityManifest,
    CollectedObservation,
    Completeness,
    CompleteSection,
    DocumentRecord,
    DurableDiscoveryBatch,
    EmptySection,
    ExcludedSection,
    NormalisedObservation,
    RetainedNativeRecord,
    RunStatus,
    SourceDefinition,
    SourceId,
    SourceReference,
    StoredCheckpoint,
    UnavailableSection,
)
from yimby.evidence import EvidenceStore
from yimby.store import SqliteStore
from yimby.transport import (
    AttachmentBodyBlockedError,
    FixtureResponse,
    FixtureSession,
    PortalRequest,
    RequestIntent,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from yimby.transport import PortalSession

WINDOW = DiscoveryWindow(
    start=date(2026, 8, 16),
    end=date(2026, 9, 15),
    include_open=True,
)
EXPECTED_SOURCE_COUNT = 2
ATTACHMENT_METRIC_DOCUMENT_URL = "https://example.test/view?document=1"


class _AttachmentThenFailurePackage:
    manifest = BARNET_PACKAGE.manifest
    _first = SourceReference(
        source_id=SourceId("barnet-idox-current"),
        reference="FIRST/1",
    )
    _second = SourceReference(
        source_id=SourceId("barnet-idox-current"),
        reference="SECOND/2",
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: StoredCheckpoint | None,
    ) -> AsyncIterator[DurableDiscoveryBatch]:
        del session, window, checkpoint
        yield DurableDiscoveryBatch(
            references=(self._first, self._second),
            next_checkpoint=StoredCheckpoint(schema_version=1, payload_json="{}"),
            complete=True,
        )

    async def collect(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> CollectedObservation:
        if reference == self._second:
            failure = "later reference failed"
            raise SourceUnavailableError(failure)
        await session.fetch(
            PortalRequest(
                url=HttpUrl(ATTACHMENT_METRIC_DOCUMENT_URL),
                intent=RequestIntent.SEARCH,
            )
        )
        return CollectedObservation(
            native_schema="AttachmentMetricNative",
            native_json="{}",
            normalised=NormalisedObservation(
                authority_id=AuthorityId("barnet"),
                reference=reference,
                proposal="First application",
                status="pending",
                documents=(
                    DocumentRecord(
                        title="Retrieved attachment",
                        url=HttpUrl(ATTACHMENT_METRIC_DOCUMENT_URL),
                    ),
                ),
                comments=(),
                completeness=Completeness(
                    application=CompleteSection(item_count=1),
                    documents=CompleteSection(item_count=1),
                    comments=EmptySection(),
                ),
                provenance=(),
                normaliser_version="attachment-metric-v1",
            ),
            evidence=(),
            observed_at=datetime(2026, 9, 16, tzinfo=UTC),
        )

    def rebuild(self, retained: RetainedNativeRecord) -> NormalisedObservation:
        del retained
        raise NotImplementedError


def _store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "yimby.sqlite3", EvidenceStore(tmp_path / "evidence"))


def _collect(
    collector: Collector,
    session: FixtureSession,
) -> tuple[ApplicationId, tuple[str, ...], int]:
    report = asyncio.run(collector.collect(AuthorityId("barnet"), WINDOW, session))
    return (
        report.applications[0],
        report.requested_urls,
        report.attachment_body_requests,
    )


def test_barnet_fixture_collection_is_idempotent_and_failure_safe(
    tmp_path: Path,
) -> None:
    """The public API preserves successful sections across retries and failure."""
    store = _store(tmp_path)
    collector = Collector(barnet_registry(), store)

    application_id, requested_urls, attachment_requests = _collect(
        collector,
        fixture_session(WINDOW),
    )

    assert requested_urls == (
        "https://publicaccess.barnet.gov.uk/online-applications/search?start=2026-08-16&end=2026-09-15&cursor=start",
        "https://publicaccess.barnet.gov.uk/online-applications/application/23/0001",
        "https://publicaccess.barnet.gov.uk/online-applications/application/23/0001/comments",
    )
    assert attachment_requests == 0
    assert store.get_application(application_id).model_dump(mode="json") == {
        "id": str(application_id),
        "authority_id": "barnet",
        "source_id": "barnet-idox-current",
        "reference": "23/0001",
        "locator": None,
        "proposal": "Build two homes & plant four trees",
        "status": "under-consideration",
        "documents": [
            {
                "title": "Site plan",
                "url": "https://publicaccess.barnet.gov.uk/online-applications/files/site-plan.pdf",
            }
        ],
        "comments": [
            {
                "comment_id": "comment-1",
                "text": "Please retain the mature trees.",
            }
        ],
        "completeness": {
            "application": {"kind": "complete", "item_count": 1},
            "documents": {"kind": "complete", "item_count": 1},
            "comments": {"kind": "complete", "item_count": 1},
        },
    }
    state = store.discovery_state(AuthorityId("barnet"))
    assert state.model_dump(mode="json") == {
        "references": ["23/0001"],
        "queued": [
            {
                "source_id": "barnet-idox-current",
                "reference": "23/0001",
                "locator": None,
            }
        ],
        "checkpoint": {
            "schema_version": 1,
            "payload_json": (
                '{"cursor":"complete","completed_queries":[],'
                '"active_query":null,"next_page":1,"query_row_count":0,'
                '"seen_references":[],"live_complete":false}'
            ),
        },
    }
    with closing(sqlite3.connect(tmp_path / "yimby.sqlite3")) as connection:
        durable_pair_count = connection.execute(
            """
            SELECT COUNT(*) FROM checkpoints
            JOIN discovery_queue
                ON discovery_queue.authority_id = checkpoints.authority_id
                AND discovery_queue.last_run_id = checkpoints.run_id
            """
        ).fetchone()
    assert durable_pair_count == (1,)
    assert sorted(path.name for path in (tmp_path / "evidence").rglob("*.gz"))
    assert any(
        b"Build two homes" in gzip.decompress(path.read_bytes())
        for path in (tmp_path / "evidence").rglob("*.gz")
    )

    repeated_id, _, repeated_attachment_requests = _collect(
        collector,
        fixture_session(WINDOW),
    )
    assert repeated_id == application_id
    assert repeated_attachment_requests == 0
    assert store.semantic_version_count(application_id, "application") == 1
    assert store.semantic_version_count(application_id, "documents") == 1
    assert store.semantic_version_count(application_id, "comments") == 1

    failed_id, _, failed_attachment_requests = _collect(
        collector,
        fixture_session(WINDOW, comments_available=False),
    )
    failed_application = store.get_application(failed_id)
    assert failed_id == application_id
    assert failed_attachment_requests == 0
    assert failed_application.comments[0].text == "Please retain the mature trees."
    assert failed_application.completeness.comments.model_dump() == {
        "kind": "failed",
        "code": "source-unavailable",
    }
    assert store.semantic_version_count(application_id, "comments") == 1
    store.close()

    reopened = _store(tmp_path)
    assert reopened.get_application(application_id).proposal == (
        "Build two homes & plant four trees"
    )
    reopened.close()


def test_failed_run_counts_attachments_retrieved_before_a_later_failure(  # noqa: D103
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    package = _AttachmentThenFailurePackage()
    collector = Collector(AuthorityRegistry((package,)), store)
    session = FixtureSession(
        {
            ATTACHMENT_METRIC_DOCUMENT_URL: FixtureResponse(
                body=b"retrieved attachment"
            ),
        }
    )

    with pytest.raises(SourceUnavailableError, match="later reference failed"):
        asyncio.run(collector.collect(AuthorityId("barnet"), WINDOW, session))

    assert store.run_statuses(AuthorityId("barnet")) == (RunStatus.FAILED,)
    assert store.metrics_totals(AuthorityId("barnet")).attachment_body_requests == 1
    store.close()


def test_empty_and_initially_failed_comments_are_explicit(tmp_path: Path) -> None:
    """Empty and failed retrievals remain distinct states."""
    empty_store = _store(tmp_path / "empty")
    empty_collector = Collector(AuthorityRegistry((BARNET_PACKAGE,)), empty_store)
    empty_id, _, _ = _collect(
        empty_collector,
        fixture_session(WINDOW, comments_html=b"<html><ul></ul></html>"),
    )
    empty = empty_store.get_application(empty_id)
    assert empty.comments == ()
    assert empty.completeness.comments.model_dump() == {"kind": "empty"}
    empty_store.close()

    failed_store = _store(tmp_path / "failed")
    failed_collector = Collector(AuthorityRegistry((BARNET_PACKAGE,)), failed_store)
    failed_id, _, _ = _collect(
        failed_collector,
        fixture_session(WINDOW, comments_available=False),
    )
    failed = failed_store.get_application(failed_id)
    assert failed.comments == ()
    assert failed.completeness.comments.model_dump() == {
        "kind": "failed",
        "code": "source-unavailable",
    }
    failed_store.close()


@pytest.mark.parametrize(
    "path",
    [
        "files/plan.pdf",
        "files/photo.png",
        "Document/Download?id=1",
    ],
)
def test_attachment_policy_blocks_body_before_fixture_lookup(path: str) -> None:
    """Document metadata URLs cannot become fixture body requests."""
    session = FixtureSession({})
    with pytest.raises(AttachmentBodyBlockedError):
        asyncio.run(
            session.fetch(
                PortalRequest(
                    url=HttpUrl(
                        f"https://publicaccess.barnet.gov.uk/online-applications/{path}"
                    ),
                    intent=RequestIntent.DETAIL,
                )
            )
        )
    assert session.requested_urls == ()
    assert session.attachment_body_requests == 1


def test_registry_and_manifest_reject_invalid_shapes() -> None:
    """The registry and manifest encode their ownership invariants."""
    assert AuthorityRegistry((BARNET_PACKAGE,)).ids() == (AuthorityId("barnet"),)
    with pytest.raises(ValueError, match="duplicate authority id"):
        AuthorityRegistry((BARNET_PACKAGE, BARNET_PACKAGE))
    with pytest.raises(ValidationError):
        AuthorityManifest(
            id=AuthorityId("invalid"),
            name="Invalid",
            kind=AuthorityKind.LONDON_BOROUGH,
            sources=(),
        )
    assert UnavailableSection(reason="not exposed").kind == "unavailable"
    assert ExcludedSection(policy="attachment bodies").kind == "excluded"


def test_unknown_application_is_not_synthesised(tmp_path: Path) -> None:
    """Inspection fails when the stable identifier is unknown."""
    store = _store(tmp_path)
    with pytest.raises(KeyError):
        store.get_application(ApplicationId("missing"))
    store.close()


def test_manifest_contains_multiple_dated_sources() -> None:
    """Barnet retains current and predecessor portal coverage."""
    sources = BARNET_PACKAGE.manifest.sources
    assert len(sources) == EXPECTED_SOURCE_COUNT
    assert sources[0] == SourceDefinition(
        id=SourceId("barnet-council-entry"),
        base_url=HttpUrl(
            "https://www.barnet.gov.uk/planning-and-building-control/"
            "planning-applications-and-permissions/view-search-and-comment"
        ),
        valid_from=date(2026, 9, 15),
    )


def test_malformed_search_fails_at_the_authority_boundary(tmp_path: Path) -> None:
    """A missing search cursor is rejected before persistence."""
    store = _store(tmp_path)
    collector = Collector(barnet_registry(), store)
    search_url = (
        "https://publicaccess.barnet.gov.uk/online-applications/search?"
        "start=2026-08-16&end=2026-09-15&cursor=start"
    )
    session = FixtureSession(
        {search_url: FixtureResponse(body=b"<html><body></body></html>")}
    )
    with pytest.raises(BarnetParseError):
        asyncio.run(collector.collect(AuthorityId("barnet"), WINDOW, session))
    assert store.discovery_state(AuthorityId("barnet")).checkpoint is None
    store.close()
