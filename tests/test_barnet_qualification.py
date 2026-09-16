# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: C901, E501, EM102, PLR0911, PLR2004, TRY003

"""Barnet durable live-qualification receipt behaviour."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl

import httpx
import pytest

import yimby.authorities.barnet.adapter as barnet_adapter
from yimby.http_transport import HostRateLimiter, HttpxPortalSession

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
    """Reject implicit access, missing active discovery, and non-30-day scopes."""
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


def test_barnet_qualification_persists_complete_typed_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Prove exact inventory, evidence, durable identities, and a zero-I/O rerun."""
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
    assert len(sessions[0].requested_urls) == 97
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
    assert len(receipt["query_inventory"]) == 19
    assert receipt["counts"]["applications"] == 19
    assert receipt["counts"]["discovered_references"] == 19
    assert receipt["counts"]["native_versions"] == 19
    assert receipt["costs"]["initial"]["request_count"] == 97
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
    assert not (data_dir / ".barnet-qualification-v1.json.tmp").exists()

    sessions.clear()
    mocks.clear()
    assert (
        module.main(
            _args(data_dir, "--resume"),
            session_factory=session_factory,
            now=lambda: now,
        )
        == 0
    )
    resumed = json.loads(capsys.readouterr().out)
    assert len(sessions) == 2
    assert all(session.requested_urls == () for session in sessions)
    assert resumed["costs"]["initial"]["request_count"] == 0
    assert resumed["costs"]["rerun"]["request_count"] == 0


def test_barnet_qualification_rejects_failed_current_sections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A malformed current section prevents any success receipt."""
    module = _qualification_module()
    data_dir = tmp_path / "failed-sections"
    sessions: list[_QualificationSession] = []

    def session_factory() -> _QualificationSession:
        session = _QualificationSession(_BarnetQualificationMock(failed_documents=True))
        sessions.append(session)
        return session

    assert module.main(_args(data_dir), session_factory=session_factory) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "qualification-failed"
    assert "failed-sections" in error["failed_checks"]
    assert len(sessions) == 1
    assert sessions[0].closed
    assert not (data_dir / "barnet-qualification-v1.json").exists()


@pytest.mark.parametrize(
    "corruption",
    ["evidence", "locator"],
)
def test_barnet_qualification_invalidates_stale_receipt_on_corruption(
    corruption: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A prior receipt cannot survive damaged evidence or identity disagreement."""
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
    else:
        with closing(sqlite3.connect(data_dir / "yimby.sqlite3")) as connection:
            connection.execute(
                "UPDATE discovery_queue SET locator = 'CORRUPTED' WHERE rowid = 1"
            )
            connection.commit()
        expected_check = "reference-application-agreement"

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
