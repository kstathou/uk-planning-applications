# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: C901, D100, D103, E501, EM101, EM102, PLR0911, PLR0915, PLR2004, SLF001, TRY003

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl

import httpx
import pytest
from pydantic import ValidationError

import yimby.authorities.barnet.adapter as barnet_adapter
import yimby.authorities.barnet.blocker as barnet_blocker
from yimby.authorities.barnet.blocker import (
    BarnetBlockerCounts,
    BarnetBlockerScope,
    BarnetQualificationBlockerV1,
    PendingBarnetCycle,
    derive_barnet_blocker,
    load_barnet_blocker,
)
from yimby.domain import AuthorityId
from yimby.evidence import EvidenceStore
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.store import SqliteStore
from yimby.transport import SourceUnavailableError

if TYPE_CHECKING:
    from types import ModuleType

START = "2026-08-18"
END = "2026-09-16"
WEEKS = (
    "17/08/2026",
    "24/08/2026",
    "31/08/2026",
    "07/09/2026",
    "14/09/2026",
)
OPEN_CASE_STATUSES = (
    "Application Received",
    "Valid Application Received",
    "Pending Consideration",
    "Pending Decision",
)
ACTIVE_APPEAL_STATUSES = (
    "Appeal in progress",
    "Appeal lodged",
    "Appeal Valid",
    "High Court Appeal Lodged",
    "Remitted to Secretary of State",
)


def _weekly_form() -> bytes:
    options = "".join(f'<option value="{week}">{week}</option>' for week in WEEKS)
    return f"""
    <form action="weeklyListResults.do?action=firstPage" method="post">
      <input type="hidden" name="_csrf" value="safe-token">
      <select name="searchCriteria.ward"><option value="" selected>All</option></select>
      <select name="week">{options}</select>
      <input type="radio" name="dateType" value="DC_Validated" checked>
      <input type="radio" name="dateType" value="DC_Decided">
      <input type="hidden" name="searchType" value="Application">
    </form>
    """.encode()


def _advanced_form() -> bytes:
    case_options = "".join(
        f'<option value="{value}">{value}</option>' for value in OPEN_CASE_STATUSES
    )
    appeal_options = "".join(
        f'<option value="{value}">{value}</option>' for value in ACTIVE_APPEAL_STATUSES
    )
    return f"""
    <form id="advancedSearchForm"
          action="advancedSearchResults.do?action=firstPage" method="post">
      <input type="hidden" name="_csrf" value="safe-token">
      <select name="searchCriteria.caseStatus">
        <option value="" selected>All</option>{case_options}
      </select>
      <select name="searchCriteria.appealStatus">
        <option value="" selected>All</option>{appeal_options}
      </select>
      <input type="hidden" name="caseAddressType" value="">
      <input type="hidden" name="searchType" value="">
      <input name="date(applicationReceivedStart)" value="">
      <input name="date(applicationReceivedEnd)" value="">
    </form>
    """.encode()


def _result(reference: str, locator: str) -> bytes:
    return (
        '<div data-result-count="1"></div><li class="searchresult">'
        f'<a href="applicationDetails.do?keyVal={locator}">Details</a>'
        f"<p>Reference: {reference}</p></li>"
    ).encode()


class _BarnetQualificationMock:
    def __init__(self, *, failed_documents: bool = False) -> None:
        self.failed_documents = failed_documents
        self.references: dict[str, str] = {}
        self.attachment_paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        action = request.url.params.get("action")
        fields = dict(parse_qsl(request.content.decode(), keep_blank_values=True))
        if path.endswith("/search.do") and action == "weeklyList":
            return httpx.Response(200, content=_weekly_form())
        if path.endswith("/weeklyListResults.do"):
            week_index = WEEKS.index(fields["week"]) + 1
            date_index = ("DC_Validated", "DC_Decided").index(fields["dateType"])
            index = (week_index - 1) * 2 + date_index + 1
            return self._record(index, prefix="WK")
        if path.endswith("/search.do") and action == "advanced":
            return httpx.Response(200, content=_advanced_form())
        if path.endswith("/advancedSearchResults.do"):
            if fields["date(applicationReceivedStart)"]:
                assert fields["date(applicationReceivedStart)"] == "18/08/2026"
                assert fields["date(applicationReceivedEnd)"] == "16/09/2026"
                return self._record(1, prefix="REC")
            selected = next(
                fields[name]
                for name in (
                    "searchCriteria.caseStatus",
                    "searchCriteria.appealStatus",
                )
                if fields[name]
            )
            index = (*OPEN_CASE_STATUSES, *ACTIVE_APPEAL_STATUSES).index(selected) + 1
            return self._record(index, prefix="ADV")
        if path.endswith("/applicationDetails.do"):
            locator = request.url.params["keyVal"]
            reference = self.references[locator]
            active_tab = request.url.params["activeTab"]
            if active_tab == "summary":
                return httpx.Response(
                    200,
                    content=(
                        '<table id="simpleDetailsTable">'
                        f"<tr><th>Reference</th><td>{reference}</td></tr>"
                        "<tr><th>Application Type</th><td>Full Planning Permission</td></tr>"
                        "<tr><th>Address</th><td>1 Qualification Road</td></tr>"
                        "<tr><th>Proposal</th><td>Qualification record</td></tr>"
                        "<tr><th>Status</th><td>Pending Consideration</td></tr>"
                        "<tr><th>Received Date</th><td>16/09/2026</td></tr>"
                        "</table>"
                    ).encode(),
                )
            if active_tab == "documents":
                if self.failed_documents:
                    return httpx.Response(
                        200,
                        content=b'<div data-section="documents" data-count="1"></div>',
                    )
                return httpx.Response(
                    200,
                    content=b'<div data-section="documents" data-count="0"></div>',
                )
            if active_tab == "neighbourComments":
                return httpx.Response(
                    200,
                    content=b'<div data-section="public comments" data-count="0"></div>',
                )
            if active_tab == "consulteeComments":
                return httpx.Response(
                    200,
                    content=b'<div data-section="consultee comments" data-count="0"></div>',
                )
        if path.endswith((".pdf", ".doc", ".docx")):
            self.attachment_paths.append(path)
            return httpx.Response(500)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def _record(self, index: int, *, prefix: str) -> httpx.Response:
        reference = f"{prefix}/{index:04d}/26"
        locator = f"{prefix}-{index}"
        self.references[locator] = reference
        return httpx.Response(200, content=_result(reference, locator))


class _RateLimitedSummaryMock(_BarnetQualificationMock):
    def __init__(self) -> None:
        super().__init__()
        self.rate_limited_summary = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (
            self.rate_limited_summary
            and request.url.path.endswith("/applicationDetails.do")
            and request.url.params["activeTab"] == "summary"
        ):
            return httpx.Response(429)
        return super().__call__(request)


class _RateLimitedAfterOneSummaryMock(_BarnetQualificationMock):
    def __init__(self) -> None:
        super().__init__()
        self.summaries = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.path.endswith("/applicationDetails.do")
            and request.url.params["activeTab"] == "summary"
        ):
            self.summaries += 1
            if self.summaries == 2:
                return httpx.Response(429)
        return super().__call__(request)


class _QualificationSession(HttpxPortalSession):
    def __init__(self, mock: _BarnetQualificationMock) -> None:
        super().__init__(
            client=httpx.AsyncClient(transport=httpx.MockTransport(mock)),
            limiter=HostRateLimiter(0),
            max_attempts=1,
        )
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


def _qualification_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "qualify_barnet.py"
    name = "_test_qualify_barnet"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _args(data_dir: Path, *extra: str) -> list[str]:
    return [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        START,
        "--end",
        END,
        "--include-open",
        *extra,
    ]


def test_barnet_qualification_requires_exact_safe_scope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    created = 0

    def session_factory() -> _QualificationSession:
        nonlocal created
        created += 1
        return _QualificationSession(_BarnetQualificationMock())

    short_scope = _args(tmp_path / "short")
    short_scope[6] = "2026-09-15"
    invalid_date = _args(tmp_path / "invalid-date")
    invalid_date[4] = "not-a-date"
    reversed_scope = _args(tmp_path / "reversed")
    reversed_scope[4] = END
    reversed_scope[6] = START
    cases = (
        (
            [
                value
                for value in _args(tmp_path / "confirmation")
                if value != "--confirm-live"
            ],
            "confirmation-required",
        ),
        (
            [value for value in _args(tmp_path / "open") if value != "--include-open"],
            "include-open-required",
        ),
        (short_scope, "thirty-day-window-required"),
        (invalid_date, "invalid-date"),
        (reversed_scope, "invalid-window"),
    )
    for arguments, expected in cases:
        assert module.main(arguments, session_factory=session_factory) == 2
        assert json.loads(capsys.readouterr().err)["error"] == expected
    assert created == 0

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing").write_text("preserve", encoding="utf-8")
    assert module.main(_args(occupied), session_factory=session_factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "resume-required"
    assert (occupied / "existing").read_text(encoding="utf-8") == "preserve"
    assert created == 0

    file_target = tmp_path / "file-target"
    file_target.write_text("preserve", encoding="utf-8")
    assert module.main(_args(file_target), session_factory=session_factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "data-dir-not-directory"
    assert file_target.read_text(encoding="utf-8") == "preserve"
    assert created == 0


def test_barnet_qualification_uses_cautious_live_transport() -> None:
    module = _qualification_module()
    session = module._default_session()
    try:
        assert session._limiter._minimum_gap == 10.0
        assert session._max_attempts == 1
    finally:
        asyncio.run(session.aclose())


def test_barnet_blocker_artifact_is_state_bound_sanitized_and_strict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "blocked"
    mock = _RateLimitedAfterOneSummaryMock()
    sessions: list[_QualificationSession] = []

    def session_factory() -> _QualificationSession:
        session = _QualificationSession(mock)
        sessions.append(session)
        return session

    assert module.main(_args(data_dir), session_factory=session_factory) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "source-unavailable"
    artifact = derive_barnet_blocker(
        data_dir,
        official_http_429_confirmed=True,
    )
    assert artifact.status == "blocked"
    assert artifact.counts.requests == len(sessions[0].requested_urls)
    assert artifact.counts.discovered_references == len(mock.references)
    assert artifact.counts.persisted_applications == 1
    assert artifact.counts.pending_retries == 1
    assert artifact.receipt_present is False
    assert artifact.sqlite_integrity == "ok"
    assert [cycle.ordinal for cycle in artifact.later_cycles] == [1, 2]

    serialized = artifact.model_dump_json()
    assert all(identity not in serialized for identity in mock.references)
    assert all(locator not in serialized for locator in mock.references.values())
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(serialized, encoding="utf-8")
    assert load_barnet_blocker(artifact_path) == artifact

    committed_path = (
        Path(__file__).parents[1]
        / "docs"
        / "evidence"
        / "barnet-qualification-blocker-2026-09-16.json"
    )
    committed_text = committed_path.read_text(encoding="utf-8")
    committed = load_barnet_blocker(committed_path)
    assert committed.counts == BarnetBlockerCounts(
        requests=26,
        discovered_references=10,
        persisted_applications=6,
        evidence_records=24,
        pending_retries=1,
    )
    assert committed.scope == BarnetBlockerScope(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=True,
    )
    assert "keyVal" not in committed_text
    assert all(
        marker not in committed_text.casefold()
        for marker in ("csrf", "cookie", "<html", "applicationdetails.do?")
    )

    payload = artifact.model_dump()
    with pytest.raises(ValidationError):
        BarnetQualificationBlockerV1.model_validate({**payload, "schema_version": "1"})
    with pytest.raises(ValidationError):
        BarnetQualificationBlockerV1.model_validate({**payload, "unexpected": True})
    with pytest.raises(ValidationError, match="timezone-aware"):
        BarnetQualificationBlockerV1.model_validate(
            {**payload, "observed_at": artifact.observed_at.replace(tzinfo=None)}
        )
    with pytest.raises(ValidationError, match="scope end"):
        BarnetQualificationBlockerV1.model_validate(
            {**payload, "observed_at": artifact.observed_at + timedelta(days=1)}
        )
    with pytest.raises(ValidationError, match="exceed discovered"):
        BarnetQualificationBlockerV1.model_validate(
            {
                **payload,
                "counts": {
                    **artifact.counts.model_dump(),
                    "persisted_applications": (
                        artifact.counts.discovered_references + 1
                    ),
                },
            }
        )
    with pytest.raises(ValidationError, match="both later cycles"):
        BarnetQualificationBlockerV1.model_validate(
            {
                **payload,
                "later_cycles": (
                    PendingBarnetCycle(ordinal=1),
                    PendingBarnetCycle(ordinal=1),
                ),
            }
        )
    with pytest.raises(ValidationError, match="exactly 30 days"):
        BarnetBlockerScope(
            start=date(2026, 8, 19),
            end=date(2026, 9, 16),
            include_open=True,
        )


def test_barnet_blocker_derivation_rejects_unverified_or_corrupt_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "blocked"
    mock = _RateLimitedAfterOneSummaryMock()

    assert (
        module.main(
            _args(data_dir),
            session_factory=lambda: _QualificationSession(mock),
        )
        == 1
    )
    capsys.readouterr()

    with pytest.raises(
        barnet_blocker.BarnetBlockerEvidenceError,
        match="confirmation-required",
    ):
        derive_barnet_blocker(data_dir, official_http_429_confirmed=False)
    with pytest.raises(
        barnet_blocker.BarnetBlockerEvidenceError,
        match="qualification-database-required",
    ):
        derive_barnet_blocker(
            tmp_path / "missing",
            official_http_429_confirmed=True,
        )
    (data_dir / "barnet-qualification-v1.json").write_text("{}", encoding="utf-8")
    with pytest.raises(
        barnet_blocker.BarnetBlockerEvidenceError,
        match="receipt-must-be-absent",
    ):
        derive_barnet_blocker(data_dir, official_http_429_confirmed=True)
    (data_dir / "barnet-qualification-v1.json").unlink()

    database = data_dir / "yimby.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        original_checkpoint = connection.execute(
            "SELECT payload_json FROM checkpoints WHERE authority_id = 'barnet'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE checkpoints SET payload_json = '{invalid' "
            "WHERE authority_id = 'barnet'"
        )
        connection.commit()
    with pytest.raises(
        barnet_blocker.BarnetBlockerEvidenceError,
        match="checkpoint-invalid",
    ):
        derive_barnet_blocker(data_dir, official_http_429_confirmed=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE checkpoints SET payload_json = ? WHERE authority_id = 'barnet'",
            (original_checkpoint,),
        )
        evidence_path = connection.execute(
            "SELECT path FROM evidence ORDER BY digest LIMIT 1"
        ).fetchone()[0]
        connection.commit()

    retained_path = data_dir / "evidence" / evidence_path
    retained_body = retained_path.read_bytes()
    retained_path.write_bytes(b"not-gzip")
    with pytest.raises(
        barnet_blocker.BarnetBlockerEvidenceError,
        match="retained-evidence-invalid",
    ):
        derive_barnet_blocker(data_dir, official_http_429_confirmed=True)
    retained_path.write_bytes(gzip.compress(b"different", mtime=0))
    with pytest.raises(
        barnet_blocker.BarnetBlockerEvidenceError,
        match="digest-mismatch",
    ):
        derive_barnet_blocker(data_dir, official_http_429_confirmed=True)
    retained_path.write_bytes(retained_body)
    assert (
        derive_barnet_blocker(
            data_dir,
            official_http_429_confirmed=True,
        ).blocker.code
        == "official-http-429"
    )


def test_barnet_qualification_persists_complete_typed_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "qualification-barnet-2026-09-16"
    sessions: list[_QualificationSession] = []
    mocks: list[_BarnetQualificationMock] = []

    def session_factory() -> _QualificationSession:
        mock = _BarnetQualificationMock()
        session = _QualificationSession(mock)
        mocks.append(mock)
        sessions.append(session)
        return session

    now = datetime(2026, 9, 16, 12, tzinfo=UTC)
    assert (
        module.main(
            _args(data_dir),
            session_factory=session_factory,
            now=lambda: now,
        )
        == 0
    )

    assert len(sessions) == 2
    assert all(session.closed for session in sessions)
    assert len(sessions[0].requested_urls) == 102
    assert sessions[1].requested_urls == ()
    assert all(mock.attachment_paths == [] for mock in mocks)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["schema_version"] == 1
    assert receipt["authority_id"] == "barnet"
    assert receipt["created_at"] == "2026-09-16T12:00:00Z"
    assert receipt["scope"] == {
        "start": START,
        "end": END,
        "include_open": True,
    }
    scope = barnet_adapter.BarnetDiscoveryScope(
        start=date.fromisoformat(receipt["scope"]["start"]),
        end=date.fromisoformat(receipt["scope"]["end"]),
        include_open=True,
    )
    assert receipt["query_inventory"] == list(
        barnet_adapter.expected_live_query_keys(scope)
    )
    assert len(receipt["query_inventory"]) == 20
    assert receipt["query_inventory"][10] == ("advanced|received|2026-08-18|2026-09-16")
    assert receipt["counts"]["applications"] == 20
    assert receipt["counts"]["discovered_references"] == 20
    assert receipt["counts"]["native_versions"] == 20
    assert receipt["costs"]["initial"]["request_count"] == 102
    assert receipt["costs"]["initial"]["attachment_body_requests"] == 0
    assert receipt["costs"]["rerun"] == {
        "request_count": 0,
        "transferred_bytes": 0,
        "attachment_body_requests": 0,
    }
    assert receipt["run_statuses"] == ["succeeded", "succeeded"]
    assert receipt["weekly_refreshes"] == [
        {"ordinal": 1, "due_on": "2026-09-23", "status": "pending"},
        {"ordinal": 2, "due_on": "2026-09-30", "status": "pending"},
    ]
    assert {check["name"]: check["ok"] for check in receipt["checks"]} == {
        "application-count": True,
        "attachment-policy": True,
        "database-integrity": True,
        "evidence-integrity": True,
        "evidence-paths": True,
        "failed-sections": True,
        "idempotent-rerun": True,
        "pending-retries": True,
        "query-inventory": True,
        "reference-application-agreement": True,
        "run-statuses": True,
        "terminal-checkpoint": True,
        "terminal-rerun-cost": True,
        "unmapped-records": True,
    }
    receipt_path = data_dir / "barnet-qualification-v1.json"
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt
    lineage_store = SqliteStore(
        data_dir / "yimby.sqlite3",
        EvidenceStore(data_dir / "evidence"),
    )
    lineage = lineage_store.qualification_lineage(
        AuthorityId("barnet"),
        "barnet-live-v1",
    )
    lineage_store.close()
    assert lineage is not None
    assert lineage.phase == "qualified"
    assert lineage.created_at == now
    assert json.loads(lineage.scope_json) == receipt["scope"]
    assert not (data_dir / ".barnet-qualification-v1.json.tmp").exists()

    victim = tmp_path / "victim"
    victim.write_text("preserve", encoding="utf-8")
    predictable_temporary = data_dir / ".barnet-qualification-v1.json.tmp"
    predictable_temporary.symlink_to(victim)
    module._write_receipt(
        receipt_path,
        module.BarnetQualificationReceiptV1.model_validate(receipt),
    )
    assert victim.read_text(encoding="utf-8") == "preserve"
    assert predictable_temporary.is_symlink()
    predictable_temporary.unlink()

    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        connection.execute("DELETE FROM qualification_lineage")
        connection.commit()
    sessions.clear()
    mocks.clear()
    assert (
        module.main(
            _args(data_dir, "--resume"),
            session_factory=session_factory,
            now=lambda: now + timedelta(days=5),
        )
        == 0
    )
    resumed = json.loads(capsys.readouterr().out)
    assert len(sessions) == 2
    assert all(session.requested_urls == () for session in sessions)
    assert resumed["costs"]["initial"]["request_count"] == 0
    assert resumed["costs"]["rerun"]["request_count"] == 0
    assert resumed["created_at"] == "2026-09-16T12:00:00Z"
    assert resumed["weekly_refreshes"] == receipt["weekly_refreshes"]
    lineage_store = SqliteStore(
        data_dir / "yimby.sqlite3",
        EvidenceStore(data_dir / "evidence"),
    )
    assert (
        lineage_store.qualification_lineage(
            AuthorityId("barnet"),
            "barnet-live-v1",
        )
        is not None
    )
    lineage_store.close()

    store = SqliteStore(
        data_dir / "yimby.sqlite3",
        EvidenceStore(data_dir / "evidence"),
    )
    refresh_reference = store.discovery_state(AuthorityId("barnet")).queued[0]
    store.enqueue_retry(AuthorityId("barnet"), refresh_reference, "scheduled-refresh")
    store.close()
    refresh_mock = _RateLimitedSummaryMock()
    assert refresh_reference.locator is not None
    refresh_mock.references[refresh_reference.locator] = refresh_reference.reference
    refresh_sessions: list[_QualificationSession] = []

    def refresh_session_factory() -> _QualificationSession:
        session = _QualificationSession(refresh_mock)
        refresh_sessions.append(session)
        return session

    assert (
        module.main(
            _args(data_dir, "--resume"),
            session_factory=refresh_session_factory,
            now=lambda: now + timedelta(days=7),
        )
        == 0
    )
    refreshed = json.loads(capsys.readouterr().out)
    assert len(refresh_sessions) == 2
    assert len(refresh_sessions[0].requested_urls) == 4
    assert refresh_sessions[1].requested_urls == ()
    assert refreshed["created_at"] == "2026-09-16T12:00:00Z"
    assert refreshed["weekly_refreshes"] == receipt["weekly_refreshes"]

    store = SqliteStore(
        data_dir / "yimby.sqlite3",
        EvidenceStore(data_dir / "evidence"),
    )
    store.enqueue_retry(AuthorityId("barnet"), refresh_reference, "scheduled-refresh")
    store.close()
    refresh_mock.rate_limited_summary = True
    refresh_sessions.clear()
    assert (
        module.main(
            _args(data_dir, "--resume"),
            session_factory=refresh_session_factory,
            now=lambda: now + timedelta(days=8),
        )
        == 1
    )
    failure = json.loads(capsys.readouterr().err)
    assert failure["error"] == "source-unavailable"
    assert "HTTP 429" in failure["detail"]
    assert not receipt_path.exists()

    refresh_mock.rate_limited_summary = False
    refresh_sessions.clear()
    assert (
        module.main(
            _args(data_dir, "--resume"),
            session_factory=refresh_session_factory,
            now=lambda: now + timedelta(days=9),
        )
        == 0
    )
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["created_at"] == "2026-09-16T12:00:00Z"
    assert recovered["weekly_refreshes"] == receipt["weekly_refreshes"]

    invalid_receipts = (
        None,
        "{invalid",
        json.dumps(
            {
                **receipt,
                "created_at": "2026-10-16T12:00:00Z",
                "weekly_refreshes": [
                    {"ordinal": 1, "due_on": "2026-10-23", "status": "pending"},
                    {"ordinal": 2, "due_on": "2026-10-30", "status": "pending"},
                ],
            }
        ),
        json.dumps(
            {
                **receipt,
                "scope": {
                    "start": "2026-08-17",
                    "end": END,
                    "include_open": True,
                },
            }
        ),
        json.dumps({**receipt, "created_at": "2026-09-16T12:00:00"}),
        json.dumps(
            {
                **receipt,
                "weekly_refreshes": [
                    {"ordinal": 1, "due_on": "2026-09-24", "status": "pending"},
                    {"ordinal": 2, "due_on": "2026-09-30", "status": "pending"},
                ],
            }
        ),
    )
    for invalid_receipt in invalid_receipts:
        if invalid_receipt is None:
            receipt_path.unlink(missing_ok=True)
        else:
            receipt_path.write_text(invalid_receipt, encoding="utf-8")
        sessions.clear()
        mocks.clear()
        assert (
            module.main(
                _args(data_dir, "--resume"),
                session_factory=session_factory,
                now=lambda: now + timedelta(days=6),
            )
            == 0
        )
        captured = capsys.readouterr()
        assert captured.err == ""
        repaired = json.loads(captured.out)
        assert repaired["created_at"] == "2026-09-16T12:00:00Z"
        assert repaired["weekly_refreshes"] == receipt["weekly_refreshes"]
        assert len(sessions) == 2
        assert all(session.requested_urls == () for session in sessions)
        assert receipt_path.exists()

    valid_scope_json = json.dumps(receipt["scope"], separators=(",", ":"))
    preserved_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    invalid_lineages = (
        ("{invalid", "2026-09-16T12:00:00+00:00"),
        (valid_scope_json, "not-a-date"),
        (valid_scope_json, "2026-10-16T12:00:00+00:00"),
        (
            json.dumps(
                {
                    "start": "2026-08-17",
                    "end": END,
                    "include_open": True,
                },
                separators=(",", ":"),
            ),
            "2026-09-16T12:00:00+00:00",
        ),
        (valid_scope_json, "2026-09-16T12:00:00"),
    )
    for scope_json, created_at in invalid_lineages:
        with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
            connection.execute(
                """
                UPDATE qualification_lineage
                SET scope_json = ?, created_at = ?
                WHERE authority_id = 'barnet'
                """,
                (scope_json, created_at),
            )
            connection.commit()
        sessions.clear()
        mocks.clear()
        assert (
            module.main(
                _args(data_dir, "--resume"),
                session_factory=session_factory,
                now=lambda: now + timedelta(days=10),
            )
            == 1
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert json.loads(captured.err) == {"error": "receipt-anchor-required"}
        assert sessions == []
        assert json.loads(receipt_path.read_text(encoding="utf-8")) == preserved_receipt

    receipt_path.unlink()
    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        connection.execute(
            """
            UPDATE qualification_lineage
            SET scope_json = ?, created_at = '1789578000'
            WHERE authority_id = 'barnet'
            """,
            (valid_scope_json,),
        )
        connection.commit()
    sessions.clear()
    assert (
        module.main(
            _args(data_dir, "--resume"),
            session_factory=session_factory,
            now=lambda: now + timedelta(days=10),
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "receipt-anchor-required"}
    assert sessions == []

    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        connection.execute(
            """
            UPDATE qualification_lineage
            SET scope_json = ?, created_at = ?
            WHERE authority_id = 'barnet'
            """,
            (valid_scope_json, "2026-09-16T12:00:00+00:00"),
        )
        connection.commit()
    conflicting_receipt = {
        **receipt,
        "created_at": "2026-09-15T12:00:00Z",
        "weekly_refreshes": [
            {"ordinal": 1, "due_on": "2026-09-22", "status": "pending"},
            {"ordinal": 2, "due_on": "2026-09-29", "status": "pending"},
        ],
    }
    receipt_path.write_text(json.dumps(conflicting_receipt), encoding="utf-8")
    sessions.clear()
    assert (
        module.main(
            _args(data_dir, "--resume"),
            session_factory=session_factory,
            now=lambda: now + timedelta(days=10),
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "receipt-anchor-required"}
    assert sessions == []


def test_barnet_qualification_recovers_after_receipt_write_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "receipt-write-failure"
    sessions: list[_QualificationSession] = []

    def session_factory() -> _QualificationSession:
        session = _QualificationSession(_BarnetQualificationMock())
        sessions.append(session)
        return session

    original_write_receipt = module._write_receipt

    def fail_receipt_write(*_args: object) -> None:
        raise OSError

    monkeypatch.setattr(module, "_write_receipt", fail_receipt_write)
    now = datetime(2026, 9, 16, 12, tzinfo=UTC)
    assert (
        module.main(
            _args(data_dir),
            session_factory=session_factory,
            now=lambda: now,
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err) == {
        "error": "runtime-failure",
        "exception": "OSError",
    }
    assert not (data_dir / "barnet-qualification-v1.json").exists()
    lineage_store = SqliteStore(
        data_dir / "yimby.sqlite3",
        EvidenceStore(data_dir / "evidence"),
    )
    lineage = lineage_store.qualification_lineage(
        AuthorityId("barnet"),
        "barnet-live-v1",
    )
    lineage_store.close()
    assert lineage is not None
    assert lineage.created_at == now

    monkeypatch.setattr(module, "_write_receipt", original_write_receipt)
    sessions.clear()
    assert (
        module.main(
            _args(data_dir, "--resume"),
            session_factory=session_factory,
            now=lambda: now + timedelta(days=1),
        )
        == 0
    )
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["created_at"] == "2026-09-16T12:00:00Z"
    assert recovered["weekly_refreshes"] == [
        {"ordinal": 1, "due_on": "2026-09-23", "status": "pending"},
        {"ordinal": 2, "due_on": "2026-09-30", "status": "pending"},
    ]
    assert len(sessions) == 2
    assert all(session.requested_urls == () for session in sessions)


def test_terminal_collection_without_lineage_can_finish_bootstrap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "terminal-without-lineage"
    sessions: list[_QualificationSession] = []

    def session_factory() -> _QualificationSession:
        session = _QualificationSession(_BarnetQualificationMock())
        sessions.append(session)
        return session

    initial_time = datetime(2026, 9, 16, 12, tzinfo=UTC)
    assert (
        module.main(
            _args(data_dir),
            session_factory=session_factory,
            now=lambda: initial_time,
        )
        == 0
    )
    capsys.readouterr()
    (data_dir / "barnet-qualification-v1.json").unlink()
    with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
        connection.execute("DELETE FROM qualification_lineage")
        connection.commit()

    sessions.clear()
    resumed_time = initial_time + timedelta(days=1)
    assert (
        module.main(
            _args(data_dir, "--resume"),
            session_factory=session_factory,
            now=lambda: resumed_time,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["created_at"] == "2026-09-17T12:00:00Z"
    assert len(sessions) == 2
    assert all(session.requested_urls == () for session in sessions)


def test_barnet_qualification_rejects_failed_current_sections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "failed-sections"
    sessions: list[_QualificationSession] = []
    mock = _BarnetQualificationMock(failed_documents=True)

    def session_factory() -> _QualificationSession:
        session = _QualificationSession(mock)
        sessions.append(session)
        return session

    assert module.main(_args(data_dir), session_factory=session_factory) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "qualification-failed"
    assert "failed-sections" in error["failed_checks"]
    assert len(sessions) == 1
    assert sessions[0].closed
    assert not (data_dir / "barnet-qualification-v1.json").exists()

    mock.failed_documents = False
    sessions.clear()
    assert (
        module.main(
            _args(data_dir, "--resume"),
            session_factory=session_factory,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["counts"]["failed_sections"] == 0
    assert receipt["counts"]["pending_retries"] == 0
    assert len(sessions) == 2
    assert sessions[0].requested_urls
    assert sessions[1].requested_urls == ()


def test_barnet_qualification_reports_source_failures_without_masking_defects(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()

    def source_failure() -> _QualificationSession:
        raise SourceUnavailableError(
            "source unavailable: https://publicaccess.barnet.gov.uk HTTP 429"
        )

    source_dir = tmp_path / "source"
    assert module.main(_args(source_dir), session_factory=source_failure) == 1
    error = json.loads(capsys.readouterr().err)
    assert error == {
        "error": "source-unavailable",
        "detail": ("source unavailable: https://publicaccess.barnet.gov.uk HTTP 429"),
    }
    assert not (source_dir / "barnet-qualification-v1.json").exists()

    def programmer_defect() -> _QualificationSession:
        raise AssertionError("programmer defect")

    defect_dir = tmp_path / "defect"
    with pytest.raises(AssertionError, match="programmer defect"):
        module.main(_args(defect_dir), session_factory=programmer_defect)
    assert capsys.readouterr().err == ""
    assert not (defect_dir / "barnet-qualification-v1.json").exists()


@pytest.mark.parametrize(
    "corruption",
    ["evidence", "historical-evidence", "locator", "checkpoint-locator"],
)
def test_barnet_qualification_invalidates_stale_receipt_on_corruption(
    corruption: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / corruption

    def session_factory() -> _QualificationSession:
        return _QualificationSession(_BarnetQualificationMock())

    assert module.main(_args(data_dir), session_factory=session_factory) == 0
    capsys.readouterr()
    receipt_path = data_dir / "barnet-qualification-v1.json"
    assert receipt_path.exists()
    if corruption == "evidence":
        evidence_path = next((data_dir / "evidence").rglob("*.gz"))
        evidence_path.write_bytes(b"not-gzip")
        expected_check = "evidence-integrity"
    elif corruption == "historical-evidence":
        digest = "f" * 64
        relative_path = f"ff/{digest}.gz"
        evidence_path = data_dir / "evidence" / relative_path
        evidence_path.parent.mkdir(parents=True)
        evidence_path.write_bytes(b"corrupt historical capture")
        with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
            connection.execute(
                """
                INSERT INTO evidence(digest, path, source_url, media_type)
                VALUES (?, ?, ?, ?)
                """,
                (
                    digest,
                    relative_path,
                    "https://publicaccess.barnet.gov.uk/online-applications/legacy",
                    "text/html",
                ),
            )
            connection.commit()
        expected_check = "evidence-integrity"
    elif corruption == "locator":
        with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
            connection.execute(
                "UPDATE discovery_queue SET locator = 'CORRUPTED' WHERE rowid = 1"
            )
            connection.commit()
        expected_check = "reference-application-agreement"
    else:
        with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
            row = connection.execute(
                "SELECT payload_json FROM checkpoints WHERE authority_id = 'barnet'"
            ).fetchone()
            assert row is not None
            checkpoint = json.loads(row[0])
            checkpoint["seen_locators"][0] = "CORRUPTED"
            connection.execute(
                "UPDATE checkpoints SET payload_json = ? WHERE authority_id = 'barnet'",
                (json.dumps(checkpoint, separators=(",", ":")),),
            )
            connection.commit()
        expected_check = "terminal-checkpoint"

    assert (
        module.main(
            _args(data_dir, "--resume"),
            session_factory=session_factory,
        )
        == 1
    )
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "qualification-failed"
    assert expected_check in error["failed_checks"]
    assert not receipt_path.exists()
