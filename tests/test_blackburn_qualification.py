# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: ANN401, D103, PLR2004

"""Persisted qualification contract for Blackburn with Darwen."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import HttpUrl

import yimby.authorities.blackburn_with_darwen.adapter as blackburn
from yimby.domain import EvidenceCapture, EvidenceDigest, TransportMode

if TYPE_CHECKING:
    from types import ModuleType


def _qualification_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "qualify_blackburn_with_darwen.py"
    name = "_test_qualify_blackburn_with_darwen"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _capture(url: str, body: bytes) -> EvidenceCapture:
    return EvidenceCapture(
        url=HttpUrl(url),
        media_type="text/html",
        body=body,
        digest=EvidenceDigest(sha256(body).hexdigest()),
    )


def _search_html(reference: str, record_id: str) -> bytes:
    return f"""
    <table id="application_results_table">
      <thead><tr><th>Application Reference</th><th>Application Type</th>
      <th>Location Details</th><th>Proposal</th><th>Ward</th>
      <th>Community</th><th>Decision</th><th>View</th></tr></thead>
      <tbody><tr><td>{reference}</td><td>Full Planning Application</td>
      <td>1 High Street</td><td>Build homes</td><td>Blackburn Central</td>
      <td>Blackburn</td><td></td><td><button class="view_application"
      data-id="{record_id}">View</button></td></tr></tbody>
    </table>
    """.encode()


def _detail_row(label: str, value: str) -> str:
    return f"""
    <div class="row"><div><strong>{label}:</strong></div>
    <div>{value}</div></div>
    """


def _detail_html(reference: str, record_id: str) -> bytes:
    fields = (
        ("Application Reference Number", reference),
        ("Application Type", "Full Planning Application"),
        ("Proposal", "Build homes"),
        ("Applicant", "Applicant One"),
        ("Agent", "Agent One"),
        ("Location", "1 High Street"),
        ("Grid Reference", "368232, 427893"),
        ("Ward", "Blackburn Central"),
        ("Parish / Community", "Blackburn"),
        ("Officer", "Officer One"),
        ("Decision Level", "Delegated"),
        ("Application Status", "Pending Consideration"),
        ("Received Date", "21-08-2026"),
        ("Valid Date", "15-09-2026"),
        ("Expiry Date", "10-11-2026"),
        ("Decision Issued Date", ""),
        ("Decision", ""),
    )
    return f"""
    <div id="application_details" data-application-id="{record_id}">
      {"".join(_detail_row(label, value) for label, value in fields)}
    </div>
    <table id="application_documents"><thead><tr>
      <th data-field-name="document_type">Document Type</th>
      <th data-field-name="description">Description</th>
      <th data-field-name="thumbnail">Thumbnail</th>
      <th data-field-name="date_document_added">Date Document Added</th>
      <th data-field-name="download">Download/View</th>
    </tr></thead><tbody><tr>
      <td data-field-name="document_type">Plan</td>
      <td data-field-name="description">Location Plan</td>
      <td data-field-name="thumbnail"></td>
      <td data-field-name="date_document_added" data-date-value="2026-09-15">
      15-09-2026</td>
      <td data-field-name="download"><a
      href="/planning/?fa=downloadDocument&amp;id={record_id}">Download</a></td>
    </tr></tbody></table>
    """.encode()


class _QualificationSession:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.searches: list[blackburn.BlackburnQueryV1] = []
        self.applications: list[blackburn.BlackburnLocatorV1] = []
        self.closed = False
        self._requested: list[str] = []
        self._bytes = 0

    async def search(
        self,
        query: blackburn.BlackburnQueryV1,
    ) -> EvidenceCapture:
        if self.fail:
            message = "sanitised live failure"
            raise RuntimeError(message)
        self.searches.append(query)
        if query.kind == blackburn.BlackburnQueryKind.OLDER_OPEN:
            reference, record_id = "10/20/0001", "170001"
        else:
            reference, record_id = "10/26/0747", "178041"
        body = _search_html(reference, record_id)
        url = "https://online.blackburn.gov.uk/planning/index.html"
        self._requested.append(url)
        self._bytes += len(body)
        return _capture(url, body)

    async def application(
        self,
        locator: blackburn.BlackburnLocatorV1,
    ) -> EvidenceCapture:
        self.applications.append(locator)
        body = _detail_html(locator.public_reference, locator.record_id)
        url = (
            "https://online.blackburn.gov.uk/planning/"
            f"index.html?fa=getApplication&id={locator.record_id}"
        )
        self._requested.append("https://online.blackburn.gov.uk/planning/index.html")
        self._bytes += len(body)
        return _capture(url, body)

    async def fetch(self, *_args: object, **_kwargs: object) -> EvidenceCapture:
        raise AssertionError

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return tuple(self._requested)

    @property
    def attachment_body_requests(self) -> int:
        return 0

    @property
    def transferred_bytes(self) -> int:
        return self._bytes

    @property
    def browser_time_ms(self) -> int:
        return len(self._requested)

    @property
    def mode(self) -> TransportMode:
        return TransportMode.BROWSER

    async def aclose(self) -> None:
        self.closed = True


def _args(data_dir: Path, *, resume: bool = False) -> list[str]:
    args = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    if resume:
        args.append("--resume")
    return args


def test_blackburn_qualification_persists_complete_zero_network_receipt(
    tmp_path: Path,
    capsys: Any,
) -> None:
    module = _qualification_module()
    sessions: list[_QualificationSession] = []

    async def session_factory() -> _QualificationSession:
        session = _QualificationSession()
        sessions.append(session)
        return session

    data_dir = tmp_path / "qualification"
    result = module.main(
        _args(data_dir),
        session_factory=session_factory,
        now=lambda: datetime(2026, 9, 16, 12, tzinfo=UTC),
    )

    assert result == 0
    assert len(sessions) == 2
    assert all(session.closed for session in sessions)
    assert len(sessions[0].requested_urls) == 6
    assert sessions[1].requested_urls == ()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["schema_version"] == 1
    assert receipt["authority_id"] == "blackburn-with-darwen"
    assert receipt["outcome"] == "qualified"
    assert receipt["scope"] == {
        "start": "2026-08-18",
        "end": "2026-09-16",
        "include_open": True,
    }
    assert receipt["counts"]["applications"] == 2
    assert receipt["counts"]["discovered_references"] == 2
    assert receipt["counts"]["failed_sections"] == 0
    assert receipt["costs"]["initial"]["request_count"] == 6
    assert receipt["costs"]["initial"]["attachment_body_requests"] == 0
    assert receipt["costs"]["rerun"] == {
        "request_count": 0,
        "transferred_bytes": 0,
        "attachment_body_requests": 0,
    }
    assert receipt["run_statuses"] == ["succeeded", "succeeded"]
    assert all(check["ok"] for check in receipt["checks"])
    assert len(receipt["query_inventory"]["roots"]) == 4
    assert len(receipt["query_inventory"]["completed"]) == 4
    assert receipt["query_inventory"]["split"] == []
    assert receipt["weekly_cycles"] == [
        {"scheduled_for": "2026-09-23", "status": "pending"},
        {"scheduled_for": "2026-09-30", "status": "pending"},
    ]
    receipt_path = data_dir / "blackburn-with-darwen-qualification-v1.json"
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt


def test_blackburn_qualification_writes_truthful_blocked_receipt(
    tmp_path: Path,
    capsys: Any,
) -> None:
    module = _qualification_module()
    sessions: list[_QualificationSession] = []

    async def session_factory() -> _QualificationSession:
        session = _QualificationSession(fail=True)
        sessions.append(session)
        return session

    data_dir = tmp_path / "blocked"
    result = module.main(
        _args(data_dir),
        session_factory=session_factory,
        now=lambda: datetime(2026, 9, 16, 12, tzinfo=UTC),
    )

    assert result == 1
    assert sessions[0].closed
    error = json.loads(capsys.readouterr().err)
    assert error == {"error": "runtime-failure", "exception": "RuntimeError"}
    receipt = json.loads(
        (data_dir / "blackburn-with-darwen-qualification-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["outcome"] == "blocked"
    assert receipt["blocker_code"] == "RuntimeError"
    assert receipt["weekly_cycles"][0]["status"] == "pending"


def test_blackburn_qualification_requires_exact_safe_scope(
    tmp_path: Path,
    capsys: Any,
) -> None:
    module = _qualification_module()
    created = 0

    async def session_factory() -> _QualificationSession:
        nonlocal created
        created += 1
        return _QualificationSession()

    args = _args(tmp_path / "wrong-window")
    args[args.index("2026-08-18")] = "2026-08-19"
    assert module.main(args, session_factory=session_factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "window-must-be-30-days"

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "preserve").write_text("yes", encoding="utf-8")
    assert module.main(_args(occupied), session_factory=session_factory) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "resume-required"
    assert (occupied / "preserve").read_text(encoding="utf-8") == "yes"
    assert created == 0
