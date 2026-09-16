# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, E501, PLR2004, SLF001

"""Cheshire East's official blocker and reliable source contracts."""

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import json
import sys
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from bs4 import BeautifulSoup
from bs4.element import Tag

import yimby.authorities.cheshire_east.adapter as cheshire
from yimby.domain import (
    DiscoveryWindow,
    EvidenceCapture,
    EvidenceDigest,
    SourceId,
    SourceReference,
    TransportMode,
)
from yimby.transport import PortalRequest, RequestMethod

if TYPE_CHECKING:
    from types import ModuleType


_ROOT = Path(__file__).parents[1]


def _search_form() -> bytes:
    return b"""
    <form id="form" name="form" method="post" action="/planning/index.html">
      <input type="hidden" name="fa" value="search">
      <input type="hidden" name="submitted" value="">
      <input name="application_reference_number" value="">
      <select name="application_type_id">
        <option value="" selected>Any</option><option value="4">Full</option>
      </select>
      <select name="decision_type_id"><option value="">Any</option></select>
      <input name="valid_date_from" value="">
      <input name="valid_date_to" value="">
      <input type="checkbox" name="ignored_checkbox" value="1">
      <input type="checkbox" name="included_checkbox" value="yes" checked>
      <input type="text" name="disabled_value" value="no" disabled>
      <textarea name="proposal">House</textarea>
      <select name="empty_multiple" multiple><option value="one">One</option></select>
      <input type="submit" name="ignored_submit" value="Search">
      <button type="submit" name="submit_button" value="Search">Search</button>
    </form>
    """


def _weekly_form() -> bytes:
    return b"""
    <form method="post" action="/planning/index.html?fa=getReceivedWeeklyList">
      <input type="text" id="week" name="week" value="14-09-2026">
      <input type="hidden" name="fa" value="">
      <button type="submit">Search</button>
    </form>
    """


def _search_results() -> bytes:
    return b"""
    <table id="application_results_table">
      <tr><th>Reference</th><th>Application Type</th><th>Location</th>
      <th>Proposal</th><th>View</th></tr>
      <tr><td>26/3335/PRIOR-1A</td><td>Prior Approval</td>
      <td>139 Abbey Road</td><td>Single storey rear extension.</td>
      <td><button class="view_application" data-id="406569">View</button></td></tr>
    </table>
    """


def _weekly_results() -> bytes:
    rows = "".join(
        f"""
        <tr><td>24/{index:04d}D</td><td>{index} High Street</td>
        <td>Proposal {index}</td><td>Ward</td><td>Community</td>
        <td></td><td></td><td>Yes</td>
        <td><a href="/planning/index.html?fa=getApplication&amp;id={400000 + index}">View</a></td></tr>
        """
        for index in range(1, 51)
    )
    return f"""
    <table>
      <tr><th>Application</th><th>Location Details</th><th>Proposal</th>
      <th>Ward</th><th>Community</th><th>Consultation End Date</th>
      <th>Publicity End Date</th><th>Details Available</th>
      <th>Jump to Application</th></tr>
      {rows}
    </table>
    """.encode()


def _detail() -> bytes:
    return b"""
    <div id="application_details" data-application-id="406569">
      <div class="row pad-bottom-5"><div><strong>Application Reference Number:</strong></div><div class="col-md-7">26/3335/PRIOR-1A</div></div>
      <div class="row pad-bottom-5"><div><strong>Application Type:</strong></div><div class="col-md-7">Prior Approval</div></div>
      <div class="row pad-bottom-5"><div><strong>Proposal:</strong></div><div class="col-md-7">Single storey rear extension.</div></div>
      <div class="row pad-bottom-5"><div><strong>Applicant:</strong></div><div class="col-md-7">Applicant One</div></div>
      <div class="row pad-bottom-5"><div><strong>Agent:</strong></div><div class="col-md-7">Agent One</div></div>
      <div class="row pad-bottom-5"><div><strong>Location:</strong></div><div class="col-md-7">139 Abbey Road</div></div>
      <div class="row pad-bottom-5"><div><strong>Grid Reference:</strong></div><div class="col-md-7">374136, 360487</div></div>
      <div class="row pad-bottom-5"><div><strong>Ward:</strong></div><div class="col-md-7">Sandbach Elworth</div></div>
      <div class="row pad-bottom-5"><div><strong>Parish / Community:</strong></div><div class="col-md-7">Sandbach</div></div>
      <div class="row pad-bottom-5"><div><strong>Officer:</strong></div><div class="col-md-7">Pending Officer Allocation</div></div>
      <div class="row pad-bottom-5"><div><strong>Application Status:</strong></div><div class="col-md-7">Pending Consideration</div></div>
      <div class="row pad-bottom-5"><div><strong>Received Date:</strong></div><div class="col-md-7">12-09-2026</div></div>
      <div class="row pad-bottom-5"><div><strong>Valid Date:</strong></div><div class="col-md-7">14-09-2026</div></div>
    </div>
    <div id="documents">
      <table id="application_documents">
        <thead><tr><th data-field-name="document_type">Document Type</th>
        <th data-field-name="description">Description</th>
        <th data-field-name="thumbnail">Thumbnail</th>
        <th data-field-name="date_document_added">Date Document Added</th>
        <th data-field-name="download">Download/View</th></tr></thead>
        <tbody><tr>
          <td data-field-name="document_type">Submitted Plans</td>
          <td data-field-name="description">Site location plan</td>
          <td data-field-name="thumbnail"><img src="https://cdn.tascomi.com/thumb.png" alt="Site location plan"></td>
          <td data-field-name="date_document_added" data-date-value="2026-09-14">14-09-2026</td>
          <td data-field-name="download"><a href="/planning/?fa=downloadDocument&amp;id=3364715&amp;public_record_id=406569">Download</a></td>
        </tr></tbody>
      </table>
      <button id="show_more_documents_application_documents" style="display:none">Show More</button>
      <button id="all_documents_loaded_application_documents" disabled>All Documents Loaded</button>
    </div>
    """


def _qualification_module() -> ModuleType:
    path = _ROOT / "scripts" / "qualify_cheshire_east.py"
    name = "_test_qualify_cheshire_east"
    spec = importlib.util.spec_from_file_location(
        name,
        path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _QualificationSession:
    def __init__(
        self,
        weekly_results: bytes | None = None,
        search_form: bytes | None = None,
        search_results: bytes | None = None,
        weekly_form: bytes | None = None,
        detail: bytes | None = None,
        media_type: str = "text/html",
    ) -> None:
        self.requests: list[PortalRequest] = []
        self._bytes = 0
        self._weekly_results = weekly_results or _weekly_results()
        self._search_form = _search_form() if search_form is None else search_form
        self._search_results = (
            b"<main><p>No Results Found</p></main>"
            if search_results is None
            else search_results
        )
        self._weekly_form = _weekly_form() if weekly_form is None else weekly_form
        self._detail = _detail() if detail is None else detail
        self._media_type = media_type

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        self.requests.append(request)
        url = str(request.url)
        if url == cheshire._SEARCH_URL and request.method == RequestMethod.GET:
            body = self._search_form
        elif url == cheshire._SEARCH_POST_URL:
            body = self._search_results
        elif (
            url == cheshire._WEEKLY_RECEIVED_URL and request.method == RequestMethod.GET
        ):
            body = self._weekly_form
        elif url == cheshire._WEEKLY_RECEIVED_URL:
            body = self._weekly_results
        elif url == cheshire._DETAIL_URL.format(locator="406569"):
            body = self._detail
        else:
            raise AssertionError(request)
        self._bytes += len(body)
        return EvidenceCapture(
            url=request.url,
            media_type=self._media_type,
            body=body,
            digest=EvidenceDigest(sha256(body).hexdigest()),
        )

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return tuple(str(request.url) for request in self.requests)

    @property
    def attachment_body_requests(self) -> int:
        return 0

    @property
    def transferred_bytes(self) -> int:
        return self._bytes

    @property
    def browser_time_ms(self) -> int:
        return 0

    @property
    def mode(self) -> TransportMode:
        return TransportMode.LIVE

    async def aclose(self) -> None:
        return None


class _UnavailableQualificationSession(_QualificationSession):
    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        self.requests.append(request)
        body = b""
        return EvidenceCapture(
            url=request.url,
            media_type="text/html",
            body=body,
            digest=EvidenceDigest(sha256(body).hexdigest()),
        )


def test_cheshire_replays_exact_successful_search_controls() -> None:
    form = cheshire.parse_search_form(_search_form())
    request = cheshire.valid_date_request(
        form,
        DiscoveryWindow(
            start=date(2026, 8, 18),
            end=date(2026, 9, 16),
            include_open=True,
        ),
    )

    assert str(request.url) == "https://pa.cheshireeast.gov.uk/planning/index.html"
    assert tuple((field.name, field.value) for field in request.form) == (
        ("fa", "search"),
        ("submitted", ""),
        ("application_reference_number", ""),
        ("application_type_id", ""),
        ("decision_type_id", ""),
        ("valid_date_from", "18-08-2026"),
        ("valid_date_to", "16-09-2026"),
        ("included_checkbox", "yes"),
        ("proposal", "House"),
    )


def test_cheshire_weekly_boundary_records_an_unproved_fifty_row_cap() -> None:
    form = cheshire.parse_weekly_form(_weekly_form())
    request = cheshire.weekly_received_request(form, date(2024, 1, 1))
    boundary = cheshire.parse_weekly_boundary(_weekly_results())

    assert tuple((field.name, field.value) for field in request.form) == (
        ("week", "01-01-2024"),
        ("fa", ""),
    )
    assert len(boundary.rows) == 50
    assert boundary.rows[0].public_reference == "24/0001D"
    assert boundary.rows[0].detail_locator == "400001"
    assert boundary.reported_total is None
    assert boundary.pagination_links == ()
    assert boundary.terminal_marker is False


def test_cheshire_detail_contract_includes_only_document_metadata() -> None:
    detail = cheshire.parse_detail_contract(
        _detail(),
        expected_reference="26/3335/PRIOR-1A",
        expected_locator="406569",
    )

    assert detail.public_reference == "26/3335/PRIOR-1A"
    assert detail.application_status == "Pending Consideration"
    assert detail.valid_date == date(2026, 9, 14)
    assert detail.grid_reference == (374136.0, 360487.0)
    assert len(detail.documents) == 1
    assert detail.documents[0].document_type == "Submitted Plans"
    assert detail.documents[0].description == "Site location plan"
    assert detail.documents[0].published_date == date(2026, 9, 14)
    assert str(detail.documents[0].url) == (
        "https://pa.cheshireeast.gov.uk/planning/"
        "?fa=downloadDocument&id=3364715&public_record_id=406569"
    )


def test_cheshire_search_and_form_failure_boundaries() -> None:
    result = cheshire.parse_search_boundary(
        b"""
        <table id="application_results_table">
        <tr><th>Reference</th><th>Application Type</th><th>Location</th>
        <th>Proposal</th><th>View</th></tr>
        <tr><td>26/1/FUL</td><td>Full</td><td>One Road</td><td>Build</td>
        <td><button class="view_application" data-id="1">View</button></td></tr>
        </table>
        """
    )
    assert result.explicit_zero is False
    assert result.results[0].public_reference == "26/1/FUL"

    for body in (b"<main></main>", b"<p>No Results Found</p><p>No Results Found</p>"):
        with pytest.raises(cheshire.CheshireEastParseError):
            cheshire.parse_search_boundary(body)

    for body in (
        _search_form().replace(
            b'action="/planning/index.html"', b'action="/planning/wrong"'
        ),
        _search_form().replace(
            b'<input name="valid_date_to" value="">',
            b'<input name="valid_date_to" value=""><input name="valid_date_to">',
        ),
        _search_form().replace(b'name="fa" value="search"', b'name="fa" value="x"'),
        _search_form().replace(
            b'name="valid_date_from" value=""',
            b'name="valid_date_from" value="" disabled',
        ),
    ):
        with pytest.raises(cheshire.CheshireEastParseError):
            cheshire.parse_search_form(body)

    custom = BeautifulSoup(
        """
        <form>
          <input type="button" name="a" value="a">
          <input type="file" name="b" value="b">
          <input type="image" name="c" value="c">
          <input type="reset" name="d" value="d">
          <input type="radio" name="e" value="e">
          <input type="radio" name="f" value="f" checked>
          <select name="g" multiple><option value="g" selected>G</option></select>
        </form>
        """,
        "html.parser",
    ).select_one("form")
    assert isinstance(custom, Tag)
    assert tuple(
        (field.name, field.value)
        for field in cheshire._successful_form_fields(custom, {})
    ) == (("f", "f"), ("g", "g"))


def test_cheshire_weekly_contract_failure_boundaries() -> None:
    counted_table = _weekly_results().replace(
        b"<table>",
        b'<table data-result-count="50">',
    )
    counted = (
        b'<section data-weekly-results="true">'
        + counted_table
        + b'<nav class="pagination"><a href="?page=2">Next</a></nav>'
        + b"<button>All Results Loaded</button></section>"
    )
    boundary = cheshire.parse_weekly_boundary(counted)
    assert boundary.reported_total == 50
    assert boundary.pagination_links == ("?page=2",)
    assert boundary.terminal_marker is True
    unrelated = (
        _weekly_results()
        + b'<aside data-result-count="50">'
        + b'<nav class="pagination"><a href="?page=2">Next</a></nav>'
        + b"<button>All Results Loaded</button></aside>"
    )
    unrelated_boundary = cheshire.parse_weekly_boundary(unrelated)
    assert unrelated_boundary.reported_total is None
    assert unrelated_boundary.pagination_links == ()
    assert unrelated_boundary.terminal_marker is False
    with_empty_table = _weekly_results().replace(b"<table>", b"<table></table><table>")
    assert len(cheshire.parse_weekly_boundary(with_empty_table).rows) == 50
    with_wrong_table = _weekly_results().replace(
        b"<table>", b"<table><tr><th>Wrong</th></tr></table><table>"
    )
    assert len(cheshire.parse_weekly_boundary(with_wrong_table).rows) == 50

    invalid_forms = (
        b"<html></html>",
        _weekly_form().replace(b'method="post"', b'method="get"'),
        _weekly_form().replace(b'name="week"', b'name="other"'),
        _weekly_form().replace(b'name="fa" value=""', b'name="fa" value="x"'),
    )
    for body in invalid_forms:
        with pytest.raises(cheshire.CheshireEastParseError):
            cheshire.parse_weekly_form(body)

    invalid_pages = (
        b"<table></table>",
        _weekly_results().replace(b"<td>Proposal 1</td>", b""),
        _weekly_results().replace(
            b'<a href="/planning/index.html?fa=getApplication&amp;id=400001">View</a>',
            b"No link",
        ),
        _weekly_results().replace(
            b"/planning/index.html?fa=getApplication&amp;id=400001",
            b"/planning/wrong?fa=getApplication&amp;id=400001",
        ),
        _weekly_results().replace(b"<table>", b'<table data-result-count="many">'),
    )
    for body in invalid_pages:
        with pytest.raises(cheshire.CheshireEastParseError):
            cheshire.parse_weekly_boundary(body)

    with pytest.raises(cheshire.CheshireEastParseError):
        cheshire.detail_request("not-numeric")


def test_cheshire_detail_contract_failure_boundaries() -> None:
    failures = (
        (
            _detail().replace(
                b'data-application-id="406569"', b'data-application-id="9"'
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(b"26/3335/PRIOR-1A", b"26/OTHER"),
            cheshire.CheshireEastReferenceMismatchError,
        ),
        (
            _detail().replace(
                b'<div class="col-md-7">Pending Officer Allocation</div>', b""
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b'</div>\n    <div id="documents">',
                b'<div class="row pad-bottom-5"><div><strong>Application Status:</strong></div><div class="col-md-7">Other</div></div></div><div id="documents">',
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(b"14-09-2026", b"not-a-date", 1),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(b"374136, 360487", b"unknown"),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b" disabled>All Documents Loaded", b">All Documents Loaded"
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(b"<thead><tr>", b"<thead><tr><th>Unexpected</th>"),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b'<td data-field-name="thumbnail">', b'<td data-field-name="wrong">'
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b'<a href="/planning/?fa=downloadDocument&amp;id=3364715&amp;public_record_id=406569">Download</a>',
                b"No link",
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(b"public_record_id=406569", b"public_record_id=9"),
            cheshire.CheshireEastParseError,
        ),
    )
    for body, error in failures:
        with pytest.raises(error):
            cheshire.parse_detail_contract(
                body,
                expected_reference="26/3335/PRIOR-1A",
                expected_locator="406569",
            )

    missing_required = _detail().replace(
        b'<div class="row pad-bottom-5"><div><strong>Valid Date:</strong></div><div class="col-md-7">14-09-2026</div></div>',
        b"",
    )
    with pytest.raises(cheshire.CheshireEastParseError):
        cheshire.parse_detail_contract(
            missing_required,
            expected_reference="26/3335/PRIOR-1A",
            expected_locator="406569",
        )
    empty_table = BeautifulSoup(_detail(), "html.parser")
    documents = empty_table.select_one("table#application_documents")
    assert isinstance(documents, Tag)
    documents.clear()
    with pytest.raises(cheshire.CheshireEastParseError):
        cheshire.parse_detail_contract(
            str(empty_table).encode(),
            expected_reference="26/3335/PRIOR-1A",
            expected_locator="406569",
        )

    cheshire._assert_window(
        cheshire.CheshireEastCheckpointV1(search_page="live"),
        DiscoveryWindow(
            start=date(2026, 8, 18),
            end=date(2026, 9, 16),
            include_open=True,
        ),
    )


def test_cheshire_live_detail_remains_unreachable_from_partial_discovery() -> None:
    adapter = cheshire.CheshireEastAdapter()
    session = _QualificationSession()

    async def exercise() -> None:
        for reference in (
            SourceReference(source_id=SourceId("other"), reference="26/1"),
            SourceReference(source_id=cheshire.SOURCE, reference="26/1"),
        ):
            with pytest.raises(cheshire.CheshireEastRoutingError):
                await adapter.fetch(session, reference)
        with pytest.raises(cheshire.CheshireEastDetailUnavailableError):
            await adapter.fetch(
                session,
                SourceReference(
                    source_id=cheshire.SOURCE,
                    reference="26/1",
                    locator="1",
                ),
            )

    asyncio.run(exercise())


def test_cheshire_blocker_receipt_is_durable_and_resumes_offline(
    tmp_path: Path,
) -> None:
    module = _qualification_module()
    sessions: list[_QualificationSession] = []

    def session_factory() -> _QualificationSession:
        session = _QualificationSession()
        sessions.append(session)
        return session

    data_dir = tmp_path / "qualification-cheshire-east-2026-09-16"
    arguments = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    result = module.main(
        arguments,
        session_factory=session_factory,
        now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
    )

    assert result == 1
    assert len(sessions) == 1
    assert len(sessions[0].requests) == 5
    receipt_path = data_dir / "cheshire-east-qualification-blocker-v2.json"
    receipt = module.CheshireEastQualificationBlockerReceiptV2.model_validate_json(
        receipt_path.read_text(encoding="utf-8")
    )
    assert receipt.outcome == "blocked"
    assert receipt.promotion_allowed is False
    assert receipt.scope.start == date(2026, 8, 18)
    assert receipt.scope.end == date(2026, 9, 16)
    assert receipt.query_inventory == (
        "source-access|search-form",
        "recent|valid|2026-08-18|2026-09-16",
        "source-access|weekly-form",
        "older-open|weekly-received|2024-01-01",
        "detail|406569",
    )
    assert tuple(request.key for request in receipt.attempted_requests) == (
        receipt.query_inventory
    )
    assert str(receipt.attempted_requests[0].url) == (
        "https://pa.cheshireeast.gov.uk/planning/index.html?fa=search"
    )
    assert receipt.attempted_requests[0].method == RequestMethod.GET
    assert receipt.attempted_requests[0].form == ()
    assert tuple(blocker.code for blocker in receipt.blockers) == (
        "recent-window-fidelity-contradicted",
        "weekly-list-terminality-unproven",
        "older-open-inventory-unproven",
    )
    assert receipt.source_contract.recent.explicit_zero is True
    assert receipt.source_contract.weekly.row_count == 50
    assert receipt.source_contract.weekly.reported_total is None
    assert receipt.source_contract.weekly.pagination_links == ()
    assert receipt.source_contract.weekly.terminal_marker is False
    assert receipt.source_contract.detail.public_reference == "26/3335/PRIOR-1A"
    assert receipt.source_contract.detail.document_count == 1
    assert receipt.costs.request_count == 5
    assert receipt.costs.attachment_body_requests == 0
    assert receipt.operational_store == "not-created"
    assert tuple(cycle.status for cycle in receipt.weekly_cycles) == (
        "pending",
        "pending",
    )
    assert not (data_dir / "yimby.sqlite3").exists()
    assert len(receipt.evidence) == 5
    for item in receipt.evidence:
        body = gzip.decompress(
            (data_dir / "evidence" / item.relative_path).read_bytes()
        )
        assert sha256(body).hexdigest() == item.digest

    def forbidden_factory() -> _QualificationSession:
        message = "offline resume constructed a portal session"
        raise AssertionError(message)

    resumed = module.main(
        [*arguments, "--resume"],
        session_factory=forbidden_factory,
        now=lambda: datetime(2026, 9, 16, 9, 1, tzinfo=UTC),
    )
    assert resumed == 1


def test_cheshire_nonzero_recent_results_remain_unproved(tmp_path: Path) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "qualification"

    result = module.main(
        [
            "--confirm-live",
            "--data-dir",
            str(data_dir),
            "--start",
            "2026-08-18",
            "--end",
            "2026-09-16",
            "--include-open",
        ],
        session_factory=lambda: _QualificationSession(search_results=_search_results()),
        now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
    )

    assert result == 1
    receipt = module.CheshireEastQualificationBlockerReceiptV2.model_validate_json(
        (data_dir / "cheshire-east-qualification-blocker-v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert tuple(blocker.code for blocker in receipt.blockers) == (
        "recent-window-terminality-unproven",
        "weekly-list-terminality-unproven",
        "older-open-inventory-unproven",
    )
    checks = {check.name: check.status for check in receipt.checks}
    assert checks["recent-window-fidelity"] == "failed"


def test_cheshire_unavailable_search_form_becomes_an_offline_blocker_receipt(
    tmp_path: Path,
) -> None:
    module = _qualification_module()
    sessions: list[_UnavailableQualificationSession] = []

    def session_factory() -> _UnavailableQualificationSession:
        session = _UnavailableQualificationSession()
        sessions.append(session)
        return session

    data_dir = tmp_path / "qualification-cheshire-east-2026-09-16"
    arguments = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    result = module.main(
        arguments,
        session_factory=session_factory,
        now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
    )

    assert result == 1
    assert len(sessions) == 1
    assert len(sessions[0].requests) == 1
    receipt_path = data_dir / "cheshire-east-qualification-blocker-v2.json"
    receipt = module.CheshireEastQualificationBlockerReceiptV2.model_validate_json(
        receipt_path.read_text(encoding="utf-8")
    )
    assert receipt.source_contract is None
    assert receipt.query_inventory == ("source-access|search-form",)
    assert receipt.pending_query_inventory == (
        "recent|valid|2026-08-18|2026-09-16",
        "source-access|weekly-form",
        "older-open|weekly-received|2024-01-01",
        "detail|406569",
    )
    assert len(receipt.attempted_requests) == 1
    assert receipt.attempted_requests[0].key == "source-access|search-form"
    assert str(receipt.attempted_requests[0].url) == (
        "https://pa.cheshireeast.gov.uk/planning/index.html?fa=search"
    )
    assert receipt.attempted_requests[0].method == RequestMethod.GET
    assert receipt.attempted_requests[0].form == ()
    assert tuple(blocker.code for blocker in receipt.blockers) == (
        "official-search-form-unavailable",
    )
    assert receipt.costs.request_count == 1
    assert receipt.costs.transferred_bytes == 0
    assert receipt.costs.attachment_body_requests == 0
    assert len(receipt.evidence) == 1
    assert not (data_dir / "yimby.sqlite3").exists()

    def forbidden_factory() -> _QualificationSession:
        message = "offline resume constructed a portal session"
        raise AssertionError(message)

    resumed = module.main(
        [*arguments, "--resume"],
        session_factory=forbidden_factory,
        now=lambda: datetime(2026, 9, 16, 9, 1, tzinfo=UTC),
    )
    assert resumed == 1


def test_cheshire_non_html_search_form_becomes_an_offline_blocker_receipt(
    tmp_path: Path,
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "qualification"
    arguments = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]

    assert (
        module.main(
            arguments,
            session_factory=lambda: _QualificationSession(
                media_type="application/xhtml+xml"
            ),
            now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
        )
        == 1
    )
    receipt = module.CheshireEastQualificationBlockerReceiptV2.model_validate_json(
        (data_dir / "cheshire-east-qualification-blocker-v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert tuple(blocker.code for blocker in receipt.blockers) == (
        "official-search-form-unavailable",
    )
    assert receipt.evidence[0].media_type == "application/xhtml+xml"

    def forbidden_factory() -> _QualificationSession:
        message = "offline resume constructed a portal session"
        raise AssertionError(message)

    assert (
        module.main(
            [*arguments, "--resume"],
            session_factory=forbidden_factory,
            now=lambda: datetime(2026, 9, 16, 9, 1, tzinfo=UTC),
        )
        == 1
    )


@pytest.mark.parametrize(
    "weekly_results",
    [
        _weekly_results().replace(b"<table>", b'<table data-result-count="100">'),
        _weekly_results().replace(
            b"</table>",
            b'</table><nav class="pagination"><a href="?page=2">Next</a></nav>',
        ),
        _weekly_results()
        .replace(b"<table>", b'<table data-result-count="100">')
        .replace(b"</table>", b"</table><button>All Results Loaded</button>"),
    ],
)
def test_cheshire_total_or_next_link_cannot_claim_weekly_terminality(
    tmp_path: Path,
    weekly_results: bytes,
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "qualification"
    result = module.main(
        [
            "--confirm-live",
            "--data-dir",
            str(data_dir),
            "--start",
            "2026-08-18",
            "--end",
            "2026-09-16",
            "--include-open",
        ],
        session_factory=lambda: _QualificationSession(weekly_results),
        now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
    )

    assert result == 1
    receipt = module.CheshireEastQualificationBlockerReceiptV2.model_validate_json(
        (data_dir / "cheshire-east-qualification-blocker-v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert "weekly-list-terminality-unproven" in {
        blocker.code for blocker in receipt.blockers
    }
    assert next(
        blocker.explanation
        for blocker in receipt.blockers
        if blocker.code == "weekly-list-terminality-unproven"
    ) == (
        "the historical weekly page does not publish an internally consistent "
        "terminal boundary"
    )
    assert (
        next(
            check.status
            for check in receipt.checks
            if check.name == "weekly-list-terminality"
        )
        == "failed"
    )


def test_cheshire_qualification_does_not_hide_programming_defects(
    tmp_path: Path,
) -> None:
    module = _qualification_module()

    class _BrokenSession(_QualificationSession):
        async def fetch(self, request: PortalRequest) -> EvidenceCapture:
            raise AssertionError(request)

    with pytest.raises(AssertionError):
        module.main(
            [
                "--confirm-live",
                "--data-dir",
                str(tmp_path / "qualification"),
                "--start",
                "2026-08-18",
                "--end",
                "2026-09-16",
                "--include-open",
            ],
            session_factory=_BrokenSession,
            now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "changed_form",
    [
        _search_form().replace(b'method="post"', b'method="get"'),
        _search_form().replace(b'name="fa" value="search"', b'name="fa" value="x"'),
        _search_form().replace(
            b'name="valid_date_from" value=""',
            b'name="valid_date_from" value="" disabled',
        ),
    ],
    ids=("method", "discriminator", "disabled-date"),
)
def test_cheshire_changed_search_contract_becomes_a_typed_blocker(
    tmp_path: Path,
    changed_form: bytes,
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "qualification"

    result = module.main(
        [
            "--confirm-live",
            "--data-dir",
            str(data_dir),
            "--start",
            "2026-08-18",
            "--end",
            "2026-09-16",
            "--include-open",
        ],
        session_factory=lambda: _QualificationSession(search_form=changed_form),
        now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
    )

    assert result == 1
    receipt = module.CheshireEastQualificationBlockerReceiptV2.model_validate_json(
        (data_dir / "cheshire-east-qualification-blocker-v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt.source_contract is None
    assert tuple(blocker.code for blocker in receipt.blockers) == (
        "official-search-form-unavailable",
    )
    assert receipt.costs.request_count == 1


@pytest.mark.parametrize(
    ("overrides", "attempted_keys"),
    [
        (
            {"search_results": b"<main></main>"},
            (
                "source-access|search-form",
                "recent|valid|2026-08-18|2026-09-16",
            ),
        ),
        (
            {"weekly_form": b"<main></main>"},
            (
                "source-access|search-form",
                "recent|valid|2026-08-18|2026-09-16",
                "source-access|weekly-form",
            ),
        ),
        (
            {"weekly_results": b"<main></main>"},
            (
                "source-access|search-form",
                "recent|valid|2026-08-18|2026-09-16",
                "source-access|weekly-form",
                "older-open|weekly-received|2024-01-01",
            ),
        ),
        (
            {"detail": b"<main></main>"},
            (
                "source-access|search-form",
                "recent|valid|2026-08-18|2026-09-16",
                "source-access|weekly-form",
                "older-open|weekly-received|2024-01-01",
                "detail|406569",
            ),
        ),
        (
            {
                "weekly_results": _weekly_results().replace(
                    b"<table>",
                    b'<table data-result-count="49">',
                )
            },
            (
                "source-access|search-form",
                "recent|valid|2026-08-18|2026-09-16",
                "source-access|weekly-form",
                "older-open|weekly-received|2024-01-01",
            ),
        ),
        (
            {
                "detail": _detail().replace(
                    b"public_record_id=406569",
                    b"public_record_id=406569&amp;extra=1",
                )
            },
            (
                "source-access|search-form",
                "recent|valid|2026-08-18|2026-09-16",
                "source-access|weekly-form",
                "older-open|weekly-received|2024-01-01",
                "detail|406569",
            ),
        ),
    ],
)
def test_cheshire_contract_drift_retains_a_resumable_blocker(
    tmp_path: Path,
    overrides: dict[str, bytes],
    attempted_keys: tuple[str, ...],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "qualification"
    arguments = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]

    result = module.main(
        arguments,
        session_factory=lambda: _QualificationSession(**overrides),
        now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
    )

    assert result == 1
    receipt = module.CheshireEastQualificationBlockerReceiptV2.model_validate_json(
        (data_dir / "cheshire-east-qualification-blocker-v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt.source_contract is None
    assert receipt.query_inventory == attempted_keys
    assert (
        tuple(request.key for request in receipt.attempted_requests) == attempted_keys
    )
    assert tuple(blocker.code for blocker in receipt.blockers) == (
        "official-source-contract-drift",
    )
    assert len(receipt.evidence) == len(attempted_keys)
    assert receipt.costs.request_count == len(attempted_keys)
    assert receipt.pending_query_inventory == tuple(
        key
        for key in (
            "source-access|search-form",
            "recent|valid|2026-08-18|2026-09-16",
            "source-access|weekly-form",
            "older-open|weekly-received|2024-01-01",
            "detail|406569",
        )
        if key not in attempted_keys
    )

    def forbidden_factory() -> _QualificationSession:
        message = "offline resume constructed a portal session"
        raise AssertionError(message)

    assert (
        module.main(
            [*arguments, "--resume"],
            session_factory=forbidden_factory,
            now=lambda: datetime(2026, 9, 16, 9, 1, tzinfo=UTC),
        )
        == 1
    )


def test_cheshire_offline_resume_rejects_semantically_tampered_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "qualification"
    arguments = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    assert (
        module.main(
            arguments,
            session_factory=_QualificationSession,
            now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
        )
        == 1
    )
    capsys.readouterr()
    receipt_path = data_dir / "cheshire-east-qualification-blocker-v2.json"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["query_inventory"] = ["detail|406569"]
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    def forbidden_factory() -> _QualificationSession:
        message = "tampered resume constructed a portal session"
        raise AssertionError(message)

    assert (
        module.main(
            [*arguments, "--resume"],
            session_factory=forbidden_factory,
            now=lambda: datetime(2026, 9, 16, 9, 1, tzinfo=UTC),
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert '"error": "runtime-failure"' in captured.err
    assert '"exception": "ValidationError"' in captured.err


@pytest.mark.parametrize(
    ("tamper_path", "replacement"),
    [
        (("attempted_requests", 0, "method"), "POST"),
        (("attempted_requests", 0, "url"), "https://example.com/evil"),
        (("attempted_requests", 0, "form"), [{"name": "evil", "value": "x"}]),
        (("attempted_requests", 1, "form", 8, "value"), "Tampered proposal"),
        (
            ("evidence", 0, "source_url"),
            "http://pa.cheshireeast.gov.uk/planning/index.html?fa=search",
        ),
        (("evidence", 0, "source_url"), "https://example.com/evil"),
        (
            ("evidence", 0, "source_url"),
            "https://pa.cheshireeast.gov.uk/planning/evil?fa=search",
        ),
        (
            ("evidence", 0, "source_url"),
            "https://pa.cheshireeast.gov.uk/planning/index.html?fa=evil",
        ),
        (("evidence", 0, "media_type"), "application/pdf"),
        (("source_contract", "recent", "visible_references"), ["26/X"]),
        (("source_contract", "weekly", "week"), "2030-01-01"),
        (("source_contract", "weekly", "row_count"), 49),
        (("source_contract", "detail", "locator"), "999999"),
        (("source_contract", "detail", "application_status"), "Fabricated"),
        (
            ("source_contract", "detail", "documents", 0, "description"),
            "Fabricated",
        ),
        (
            ("source_contract", "detail", "documents", 0, "url"),
            "https://example.com/evil",
        ),
    ],
    ids=(
        "request-method",
        "request-url",
        "request-form",
        "request-form-body-binding",
        "evidence-url-scheme",
        "evidence-url-host",
        "evidence-url-path",
        "evidence-url-query",
        "evidence-media-type",
        "recent-zero-with-reference",
        "historical-week",
        "weekly-row-count",
        "detail-locator",
        "detail-status",
        "document-description",
        "document-url",
    ),
)
def test_cheshire_offline_resume_binds_requests_evidence_and_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tamper_path: tuple[str | int, ...],
    replacement: object,
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "qualification"
    arguments = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    assert (
        module.main(
            arguments,
            session_factory=_QualificationSession,
            now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
        )
        == 1
    )
    capsys.readouterr()
    receipt_path = data_dir / "cheshire-east-qualification-blocker-v2.json"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    target = payload
    for component in tamper_path[:-1]:
        target = target[component]
    target[tamper_path[-1]] = replacement
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    def forbidden_factory() -> _QualificationSession:
        message = "tampered resume constructed a portal session"
        raise AssertionError(message)

    assert (
        module.main(
            [*arguments, "--resume"],
            session_factory=forbidden_factory,
            now=lambda: datetime(2026, 9, 16, 9, 1, tzinfo=UTC),
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert '"error": "runtime-failure"' in captured.err


def test_cheshire_offline_resume_accepts_transport_sanitized_query(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "qualification"
    arguments = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    assert (
        module.main(
            arguments,
            session_factory=_QualificationSession,
            now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
        )
        == 1
    )
    capsys.readouterr()
    receipt_path = data_dir / "cheshire-east-qualification-blocker-v2.json"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    for item in payload["evidence"]:
        item["source_url"] = item["source_url"].partition("?")[0]
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    def forbidden_factory() -> _QualificationSession:
        message = "offline resume constructed a portal session"
        raise AssertionError(message)

    assert (
        module.main(
            [*arguments, "--resume"],
            session_factory=forbidden_factory,
            now=lambda: datetime(2026, 9, 16, 9, 1, tzinfo=UTC),
        )
        == 1
    )
    captured = capsys.readouterr()
    assert '"outcome":"blocked"' in captured.out
    assert captured.err == ""


def test_cheshire_resume_without_receipt_refuses_source_io(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()

    def forbidden_factory() -> _QualificationSession:
        message = "missing receipt constructed a portal session"
        raise AssertionError(message)

    result = module.main(
        [
            "--confirm-live",
            "--data-dir",
            str(tmp_path / "qualification"),
            "--start",
            "2026-08-18",
            "--end",
            "2026-09-16",
            "--include-open",
            "--resume",
        ],
        session_factory=forbidden_factory,
        now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
    )

    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert '"error": "receipt-required"' in captured.err
