# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, PLR2004, SLF001

"""Official API pagination, evidence, migration, and refresh contracts."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
import pytest

from yimby.authorities.camden import CamdenPackage
from yimby.authorities.camden import open_data as api
from yimby.authorities.camden.discovery import CAMDEN_SOURCE
from yimby.authorities.camden.fixtures import fixture_session
from yimby.collection import Collector
from yimby.domain import (
    AuthorityId,
    DiscoveryWindow,
    SourceId,
    SourceReference,
    StoredCheckpoint,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.orchestration import LiveSessionFactory
from yimby.registry import AuthorityRegistry, pilot_registry
from yimby.store import SqliteStore

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from yimby.domain import DiscoveryBatch

WINDOW = DiscoveryWindow(start=date(2026, 8, 18), end=date(2026, 9, 16))


def row(pk: int, **changes: object) -> dict[str, object]:
    return {
        "pk": str(pk),
        "application_number": f"2026/{pk}/P",
        "development_description": "Two homes",
        "development_address": "1 High St",
        "system_status": "Registered",
        "socrata_id": str(pk),
        "last_uploaded": "2026-09-16T02:30:00.000",
        **changes,
    }


class Feed:
    """Deterministic SODA responses with injectable failures."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        """Retain rows and query history."""
        self.rows = rows
        self.requests: list[httpx.Request] = []
        self.summary_override: object | None = None
        self.page_override: list[dict[str, object]] | None = None
        self.drift = False
        self.summaries = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Respond to aggregate, keyset and exact-reference queries."""
        self.requests.append(request)
        assert request.url.host == "opendata.camden.gov.uk"
        assert request.url.path == "/resource/2eiu-s2cw.json"
        query = request.url.params
        if "$select" in query:
            self.summaries += 1
            total = len({str(item["pk"]) for item in self.rows})
            payload = (
                self.summary_override
                if self.summary_override is not None
                else [
                    {
                        "total": str(total),
                        "references": str(total),
                        "watermark": "changed"
                        if self.drift and self.summaries > 1
                        else "stamp",
                    }
                ]
            )
        elif "pk >" in query["$where"]:
            cursor = int(query["$where"].rsplit("pk >", 1)[1])
            payload = (
                self.page_override
                if self.page_override is not None
                else [item for item in self.rows if int(str(item["pk"])) > cursor][
                    : int(query["$limit"])
                ]
            )
        else:
            payload = self.rows
        return httpx.Response(200, json=payload)


def session(feed: Feed) -> HttpxPortalSession:
    return HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(feed)),
        limiter=HostRateLimiter(minimum_gap=0),
    )


async def discover(
    feed: Feed, checkpoint: api.CamdenOpenDataCheckpointV1 | None = None
) -> list[DiscoveryBatch[api.CamdenOpenDataCheckpointV1]]:
    client = session(feed)
    try:
        return [
            batch
            async for batch in api.CamdenOpenDataAdapter().discover(
                client, WINDOW, checkpoint
            )
        ]
    finally:
        await client.aclose()


def test_bulk_collection_duplicate_boundary_refresh_and_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "PAGE_SIZE", 3)
    feed = Feed([row(1), row(2), row(2, socrata_id="22"), row(3)])
    package = CamdenPackage()
    store = SqliteStore(tmp_path / "db", EvidenceStore(tmp_path / "evidence"))
    collector = Collector(AuthorityRegistry((package,)), store)

    async def collect() -> None:
        for _ in range(2):
            client = session(feed)
            try:
                report = await collector.collect(AuthorityId("camden"), WINDOW, client)
                assert len(report.applications) == 3
                assert len(client.requested_urls) == 5
                assert client.attachment_body_requests == 0
            finally:
                await client.aclose()

    try:
        asyncio.run(collect())
        assert len(store.authority_application_ids(AuthorityId("camden"))) == 3
        assert store.qualification_snapshot(AuthorityId("camden")).native_versions == 3
        assert store.evidence_integrity(AuthorityId("camden")).issues == ()
        for retained in store.retained_native_records():
            rebuilt = package.rebuild(retained)
            assert rebuilt.completeness.documents.kind == "unavailable"
            assert rebuilt.completeness.comments.kind == "unavailable"
            assert rebuilt.status == "registered"
    finally:
        store.close()


def test_resume_and_terminal_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "PAGE_SIZE", 2)
    feed = Feed([row(1), row(2), row(3)])

    async def first() -> api.CamdenOpenDataCheckpointV1:
        client = session(feed)
        generator = cast(
            "AsyncGenerator[DiscoveryBatch[api.CamdenOpenDataCheckpointV1]]",
            api.CamdenOpenDataAdapter().discover(client, WINDOW, None),
        )
        try:
            return (await anext(generator)).next_checkpoint
        finally:
            await generator.aclose()
            await client.aclose()

    checkpoint = asyncio.run(first())
    assert checkpoint.last_pk == 1
    batches = asyncio.run(discover(feed, checkpoint))
    assert [ref.reference for batch in batches for ref in batch.references] == [
        "2026/2/P",
        "2026/3/P",
    ]
    assert batches[-1].next_checkpoint.enumerated == 3
    assert len(asyncio.run(discover(feed, batches[-1].next_checkpoint))) == 3
    with pytest.raises(api.CamdenOpenDataError, match="unfinished"):
        asyncio.run(discover(feed, checkpoint.model_copy(update={"watermark": "old"})))


@pytest.mark.parametrize(
    "failure",
    [
        "summary",
        "identity",
        "order",
        "cursor",
        "capacity",
        "duplicates",
        "drift",
        "missing",
    ],
)
def test_discovery_fails_closed(failure: str, monkeypatch: pytest.MonkeyPatch) -> None:
    feed = Feed([row(1), row(2)])
    if failure == "summary":
        feed.summary_override = []
    elif failure == "identity":
        feed.summary_override = [{"total": "2", "references": "1"}]
    elif failure == "order":
        feed.page_override = [row(2), row(1)]
    elif failure == "cursor":
        feed.page_override = [row(0)]
    elif failure == "capacity":
        monkeypatch.setattr(api, "PAGE_SIZE", 2)
        feed.rows = [row(1), row(1)]
    elif failure == "duplicates":
        feed.rows = [row(1), row(1, development_description="Changed")]
    elif failure == "drift":
        feed.drift = True
    else:
        feed.page_override = []
    checkpoint = (
        api.CamdenOpenDataCheckpointV1(
            window=WINDOW, expected=2, watermark="stamp", last_pk=0
        )
        if failure == "cursor"
        else None
    )
    with pytest.raises(api.CamdenOpenDataError):
        asyncio.run(discover(feed, checkpoint))


def test_empty_and_invalid_windows() -> None:
    batches = asyncio.run(discover(Feed([])))
    assert batches[0].complete
    assert batches[0].references == ()
    assert "Registered" not in api.scope_filter(
        WINDOW.model_copy(update={"include_open": False})
    )
    with pytest.raises(api.CamdenOpenDataError, match="end precedes"):
        api.scope_filter(WINDOW.model_copy(update={"end": date(2025, 1, 1)}))


@pytest.mark.parametrize(
    "failure", ["source", "missing", "capacity", "conflict", "reference", "locator"]
)
def test_exact_reference_fails_closed(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed = Feed([row(1)])
    reference = SourceReference(
        source_id=CAMDEN_SOURCE, reference="2026/1/P", locator="1"
    )
    if failure == "source":
        reference = reference.model_copy(update={"source_id": SourceId("wrong")})
    elif failure == "missing":
        feed.rows = []
    elif failure == "capacity":
        monkeypatch.setattr(api, "PAGE_SIZE", 1)
    elif failure == "conflict":
        feed.rows.append(row(1, system_status="Withdrawn"))
    elif failure == "reference":
        reference = reference.model_copy(update={"reference": "wrong"})
    else:
        reference = reference.model_copy(update={"locator": "2"})

    async def run() -> None:
        client = session(feed)
        try:
            await api.CamdenOpenDataAdapter().fetch(client, reference)
        finally:
            await client.aclose()

    with pytest.raises(api.CamdenOpenDataError):
        asyncio.run(run())


def test_exact_normalisation_preserves_dates_and_unknowns() -> None:
    feed = Feed(
        [
            row(
                1,
                system_status=None,
                easting="530748",
                northing="182755",
                valid_from_date="2026-08-18T00:00:00",
                decision_date="2026-09-15T00:00:00",
                full_application={
                    "url": "https://planningrecords.camden.gov.uk/example"
                },
            )
        ]
    )

    async def run() -> None:
        client = session(feed)
        adapter = api.CamdenOpenDataAdapter()
        try:
            snapshot = await adapter.fetch(
                client, SourceReference(source_id=CAMDEN_SOURCE, reference="2026/1/P")
            )
            observation = adapter.normalise(snapshot)
            assert observation.status == "unknown"
            assert observation.metadata.location is not None
            assert observation.metadata.validated_date == WINDOW.start
            assert observation.metadata.decision_date == date(2026, 9, 15)
        finally:
            await client.aclose()

    asyncio.run(run())


def test_old_checkpoint_restarts_on_api() -> None:
    async def run() -> None:
        client = session(Feed([row(1)]))
        try:
            batches = [
                batch
                async for batch in CamdenPackage().discover(
                    client,
                    WINDOW,
                    StoredCheckpoint(schema_version=1, payload_json='{"kind":"live"}'),
                )
            ]
            assert batches[-1].complete
        finally:
            await client.aclose()

    asyncio.run(run())


def test_token_header_and_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAMDEN_SOCRATA_APP_TOKEN", "test-secret")
    client = api.create_session()
    assert client._client.headers["X-App-Token"] == "test-secret"
    assert "test-secret" not in str(api.api_url(**{"$limit": "1"}))
    asyncio.run(client.aclose())
    live_client = asyncio.run(
        LiveSessionFactory(pilot_registry())(AuthorityId("camden"))
    )
    assert isinstance(live_client, HttpxPortalSession)
    asyncio.run(live_client.aclose())
    monkeypatch.delenv("CAMDEN_SOCRATA_APP_TOKEN")
    client = api.create_session()
    assert "X-App-Token" not in client._client.headers
    asyncio.run(client.aclose())


def test_legacy_retained_rebuild(tmp_path: Path) -> None:
    package = CamdenPackage()
    store = SqliteStore(tmp_path / "db", EvidenceStore(tmp_path / "evidence"))
    try:
        asyncio.run(
            Collector(AuthorityRegistry((package,)), store).collect(
                AuthorityId("camden"), WINDOW, fixture_session(WINDOW)
            )
        )
        for retained in store.retained_native_records():
            assert package.rebuild(retained).reference == retained.reference
    finally:
        store.close()


def test_abandoned_page_cache_does_not_cross_sessions() -> None:
    async def run() -> None:
        adapter = api.CamdenOpenDataAdapter()
        old = session(Feed([row(1)]))
        fresh_feed = Feed([row(1, development_description="Updated proposal")])
        fresh = session(fresh_feed)
        try:
            batches = [batch async for batch in adapter.discover(old, WINDOW, None)]
            snapshot = await adapter.fetch(fresh, batches[0].references[0])
            assert snapshot.payload.development_description == "Updated proposal"
            assert len(fresh_feed.requests) == 1
        finally:
            await old.aclose()
            await fresh.aclose()

    asyncio.run(run())


def test_api_qualification_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = Path(__file__).parents[1] / "scripts" / "qualify_camden.py"
    spec = importlib.util.spec_from_file_location("_test_camden_api_qualify", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    feed = Feed([row(1)])
    monkeypatch.setattr(module, "create_session", lambda: session(feed))
    arguments = [
        "--data-dir",
        str(tmp_path / "run"),
        "--start",
        WINDOW.start.isoformat(),
        "--end",
        WINDOW.end.isoformat(),
        "--include-open",
    ]
    with pytest.raises(SystemExit):
        module.main(arguments)
    assert not feed.requests
    assert module.main([*arguments, "--confirm-live"]) == 0
    receipt = json.loads(
        (tmp_path / "run" / "camden-open-data-qualification.json").read_text()
    )
    assert receipt["immediate_refresh_unchanged"]
    assert receipt["passes"][0]["source_applications"] == 1
    assert receipt["passes"][1]["requests"] == 3
    with pytest.raises(SystemExit):
        module.main([*arguments, "--confirm-live"])
    assert module.main([*arguments, "--confirm-live", "--resume"]) == 0
