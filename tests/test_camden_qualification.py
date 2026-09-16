# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, E501, PLR2004

"""Camden live qualification acceptance behavior."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

import pytest  # noqa: TC002 - Runtime assertions use pytest.raises.

from yimby.authorities.camden import discovery
from yimby.authorities.camden.adapter import DOCUMENT_BASE
from yimby.domain import EvidenceCapture, EvidenceDigest, TransportMode
from yimby.transport import PortalRequest, RequestMethod

if TYPE_CHECKING:
    from types import ModuleType


def _qualification_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "qualify_camden.py"
    name = "_test_qualify_camden"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _search_form() -> bytes:
    return b"""
    <form id="M3Form" method="post" action="GeneralSearch.aspx">
      <input type="hidden" name="__VIEWSTATE" value="state">
      <input name="txtApplicationNumber"><input name="txtApplicantName">
      <input name="txtAgentName"><input name="txtSiteAddress">
      <select name="cboStreetReferenceNumber"><option value="" selected>All</option></select>
      <input name="txtProposal">
      <select name="cboWardCode"><option value="" selected>All</option></select>
      <select name="cboApplicationTypeCode"><option value="" selected>All</option></select>
      <select name="cboDevelopmentTypeCode"><option value="" selected>All</option></select>
      <select name="cboStatusCode"><option value="" selected>All</option><option value="4">REGISTERED</option><option value="14">APPEAL LODGED</option></select>
      <select name="cboSelectDateValue"><option value="DATE_RECEIVED" selected>Received</option><option value="DATE_VALID">Valid</option><option value="DATE_DECISION">Decision</option></select>
      <input type="radio" name="rbGroup" value="rbRange">
      <input name="dateStart"><input name="dateEnd">
      <input type="radio" name="rbGroup" value="rbNotApplicable" checked>
      <input type="hidden" name="edrDateSelection" value="">
      <input type="submit" name="csbtnSearch" value="Search">
    </form>
    """


def _result(reference: str, locator: str) -> bytes:
    return f"""
    <span id="lblPagePosition">Record 1 of 1</span>
    <table summary="Results of the Search"><tr><th>Application Number</th></tr>
      <tr><td title="View Application Details"><a href="StdDetails.aspx?PARAM0={locator}&amp;PUBLIC=Y">{reference}</a></td></tr>
    </table>
    """.encode()


def _detail(reference: str) -> bytes:
    return f"""
    <div class="dataview"><h2>Application Details</h2><ul>
      <li><div><span>Application Number</span>{reference}</div></li>
      <li><div><span>Proposal</span>Qualify {reference}</div></li>
      <li><div><span>Current Status</span>REGISTERED</div></li>
      <li><div><span>Location Co ordinates</span>Easting 530748 Northing 182755</div></li>
    </ul></div>
    """.encode()


def _documents(*, malformed: bool) -> bytes:
    reported = 1 if malformed else 0
    return f"""
    <table id="casefilesummary"><tr><td><label>Records:</label></td><td>{reported}</td></tr></table>
    <table id="recordtable"><thead><tr><th>Date Created</th><th>Title</th><th>Document Type</th></tr></thead><tbody></tbody></table>
    """.encode()


class _Portal:
    def __init__(self, *, malformed_documents: bool = False) -> None:
        self.malformed_documents = malformed_documents
        self.by_locator = {
            "1001": "2026/1/P",
            "2001": "TP/TP/12531/180693",
            "3001": "2025/9999/A",
        }

    def __call__(self, request: PortalRequest) -> bytes:
        url = str(request.url)
        if request.method == RequestMethod.GET and url == discovery.GENERAL_SEARCH_URL:
            return _search_form()
        if request.method == RequestMethod.POST:
            fields = {field.name: field.value for field in request.form}
            if fields["rbGroup"] == "rbNotApplicable":
                return (
                    _result("TP/TP/12531/180693", "2001")
                    if fields["cboStatusCode"] == "4"
                    else _result("2025/9999/A", "3001")
                )
            return _result("2026/1/P", "1001")
        if "/Redirection/redirect.aspx" in url:
            locator = dict(parse_qsl(urlsplit(url).query))["PARAM0"]
            return _detail(self.by_locator[locator])
        if url.startswith(DOCUMENT_BASE):
            return _documents(malformed=self.malformed_documents)
        raise AssertionError(url)


class _Session:
    def __init__(self, portal: _Portal) -> None:
        self.portal = portal
        self.requests: list[PortalRequest] = []
        self._bytes = 0
        self.closed = False

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        self.requests.append(request)
        body = self.portal(request)
        self._bytes += len(body)
        return EvidenceCapture(
            url=request.url,
            media_type="text/html",
            body=body,
            digest=EvidenceDigest(sha256(body).hexdigest()),
        )

    @property
    def mode(self) -> TransportMode:
        return TransportMode.LIVE

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return tuple(str(request.url) for request in self.requests)

    @property
    def transferred_bytes(self) -> int:
        return self._bytes

    @property
    def attachment_body_requests(self) -> int:
        return 0

    @property
    def browser_time_ms(self) -> int:
        return 0

    async def aclose(self) -> None:
        self.closed = True


def _args(data_dir: Path) -> list[str]:
    return [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]


def test_camden_qualification_requires_exact_safe_scope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    created = 0

    def factory() -> _Session:
        nonlocal created
        created += 1
        return _Session(_Portal())

    missing_confirmation = _args(tmp_path / "confirmation")[1:]
    assert module.main(missing_confirmation, session_factory=factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "confirmation-required"

    wrong_window = _args(tmp_path / "window")
    wrong_window[wrong_window.index("2026-08-18")] = "2026-08-19"
    assert module.main(wrong_window, session_factory=factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "30-day-window-required"

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "preserve").write_text("yes", encoding="utf-8")
    assert module.main(_args(occupied), session_factory=factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "resume-required"
    assert created == 0


def test_camden_qualification_writes_proof_receipt_and_zero_io_rerun(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "qualification"
    sessions: list[_Session] = []

    def factory() -> _Session:
        session = _Session(_Portal())
        sessions.append(session)
        return session

    result = module.main(
        _args(data_dir),
        session_factory=factory,
        now=lambda: datetime(2026, 9, 16, 12, tzinfo=UTC),
    )

    assert result == 0
    assert len(sessions) == 2
    assert all(session.closed for session in sessions)
    assert len(sessions[0].requested_urls) == 16
    assert sessions[1].requested_urls == ()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["schema_version"] == 1
    assert receipt["authority_id"] == "camden"
    assert [item["reported_count"] for item in receipt["query_results"]] == [
        1,
        1,
        1,
        1,
        1,
    ]
    assert [item["query"]["kind"] for item in receipt["query_results"]] == [
        "date",
        "date",
        "date",
        "status",
        "status",
    ]
    agreement = receipt["reference_agreement"]
    assert agreement["checkpoint_count"] == 3
    assert agreement["discovery_count"] == 3
    assert agreement["application_count"] == 3
    assert agreement["rebuild_input_count"] == 3
    assert agreement["exact_match"] is True
    assert receipt["evidence_integrity"]["captures_checked"] == 4
    assert receipt["evidence_integrity"]["issues"] == []
    assert receipt["costs"]["rerun"] == {
        "request_count": 0,
        "transferred_bytes": 0,
        "attachment_body_requests": 0,
    }
    assert receipt["run_statuses"] == ["succeeded", "succeeded"]
    assert receipt["weekly_refresh_cycles"] == [
        {"ordinal": 1, "due_on": "2026-09-23", "status": "pending"},
        {"ordinal": 2, "due_on": "2026-09-30", "status": "pending"},
    ]
    assert all(check["ok"] for check in receipt["checks"])
    stored = json.loads(
        (data_dir / "camden-qualification-v1.json").read_text(encoding="utf-8")
    )
    assert stored == receipt
    assert not (data_dir / ".camden-qualification-v1.json.tmp").exists()


def test_camden_qualification_refuses_failed_document_sections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "failed"
    sessions: list[_Session] = []

    def factory() -> _Session:
        session = _Session(_Portal(malformed_documents=True))
        sessions.append(session)
        return session

    assert module.main(_args(data_dir), session_factory=factory) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "qualification-failed"
    assert "failed-sections" in error["failed_checks"]
    assert len(sessions) == 1
    assert not (data_dir / "camden-qualification-v1.json").exists()
