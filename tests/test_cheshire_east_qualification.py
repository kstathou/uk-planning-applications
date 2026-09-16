# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D100, E501, PLR0915, PLR2004, SLF001

from __future__ import annotations

import asyncio
import gzip
import importlib
import json
import os
import stat
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import HttpUrl, ValidationError

import yimby.authorities.cheshire_east.adapter as cheshire
from yimby.domain import (
    DiscoveryWindow,
    EvidenceCapture,
    EvidenceDigest,
    SourceId,
    SourceReference,
    TransportMode,
)
from yimby.evidence import EvidenceStore
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.transport import (
    AttachmentBodyBlockedError,
    PortalRequest,
    RequestIntent,
    RequestMethod,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
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
      <input type="hidden" name="site_address_x" value="999999">
      <input type="hidden" name="site_address_y" value="999999">
      <select name="ps_development_code_id" multiple>
        <option selected value="narrow">Narrow</option>
      </select>
      <input name="valid_date_from" value="">
      <input name="valid_date_to" value="">
      <input type="text" name="disabled_value" value="no" disabled>
      <textarea name="proposal">House</textarea>
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
    <div class="centered application-list">
    <table id="application_results_table">
      <tr><th>Reference</th><th>Application Type</th><th>Location</th>
      <th>Proposal</th><th>View</th></tr>
      <tr><td>26/3335/PRIOR-1A</td><td>Prior Approval</td>
      <td>139 Abbey Road</td><td>Single storey rear extension.</td>
      <td><button class="view_application" data-id="406569">View</button></td></tr>
    </table>
    </div>
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
    return importlib.reload(importlib.import_module("yimby.cheshire_qualification"))


_SEARCH_FORM_URL = str(cheshire.search_form_request().url)
_SEARCH_POST_URL = "https://pa.cheshireeast.gov.uk/planning/index.html"
_WEEKLY_URL = str(cheshire.weekly_received_form_request().url)
_DETAIL_URL = str(cheshire.detail_request("406569").url)


class _QualificationSession:
    def __init__(
        self,
        weekly_results: bytes | None = None,
        search_form: bytes | None = None,
        search_results: bytes | None = None,
        weekly_form: bytes | None = None,
        detail: bytes | None = None,
    ) -> None:
        self.requests: list[PortalRequest] = []
        self._bytes = 0
        self._weekly_results = weekly_results or _weekly_results()
        self._search_form = _search_form() if search_form is None else search_form
        self._search_results = (
            b'<div class="col-sm-12 col-md-12 animation-fadeIn application-list">'
            b'<div class="push-30-t"><strong class="text-danger">'
            b"No Results Found.</strong></div></div>"
            if search_results is None
            else search_results
        )
        self._weekly_form = _weekly_form() if weekly_form is None else weekly_form
        self._detail = _detail() if detail is None else detail

    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        self.requests.append(request)
        url = str(request.url)
        if url == _SEARCH_FORM_URL and request.method == RequestMethod.GET:
            body = self._search_form
        elif url == _SEARCH_POST_URL:
            body = self._search_results
        elif url == _WEEKLY_URL and request.method == RequestMethod.GET:
            body = self._weekly_form
        elif url == _WEEKLY_URL:
            body = self._weekly_results
        elif url == _DETAIL_URL:
            body = self._detail
        else:
            raise AssertionError(request)
        self._bytes += len(body)
        return EvidenceCapture(
            url=request.url,
            media_type="text/html",
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


class _NonHtmlQualificationSession(_QualificationSession):
    async def fetch(self, request: PortalRequest) -> EvidenceCapture:
        capture = await super().fetch(request)
        return capture.model_copy(update={"media_type": "application/xhtml+xml"})


@pytest.mark.parametrize(
    "media_type",
    [
        None,
        "application/x-pdf",
        "Application/X-PDF; charset=binary",
        "application/x-bin",
    ],
)
def test_cheshire_http_transport_rejects_unknown_media_before_body_read(
    media_type: str | None,
) -> None:
    body_reads = 0

    class ForbiddenStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal body_reads
            body_reads += 1
            yield b"must not be read"

    class ForbiddenTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self,
            request: httpx.Request,
        ) -> httpx.Response:
            headers = {} if media_type is None else {"content-type": media_type}
            return httpx.Response(
                200,
                headers=headers,
                stream=ForbiddenStream(),
                request=request,
            )

    session = HttpxPortalSession(
        client=httpx.AsyncClient(transport=ForbiddenTransport()),
        limiter=HostRateLimiter(0),
    )

    async def exercise() -> None:
        request = PortalRequest(
            url=HttpUrl("https://example.test/search"),
            intent=RequestIntent.SEARCH,
        )
        with pytest.raises(AttachmentBodyBlockedError, match=r"example\.test"):
            await session.fetch(request)
        await session.aclose()

    asyncio.run(exercise())
    assert body_reads == 0
    assert session.transferred_bytes == 0
    assert session.attachment_body_requests == 1


def test_cheshire_http_transport_blocks_query_attachment_before_request() -> None:
    requests = 0

    class ForbiddenTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self,
            request: httpx.Request,
        ) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"must not be read",
                request=request,
            )

    session = HttpxPortalSession(
        client=httpx.AsyncClient(transport=ForbiddenTransport()),
        limiter=HostRateLimiter(0),
    )

    async def exercise() -> None:
        request = PortalRequest(
            url=HttpUrl(
                "https://pa.cheshireeast.gov.uk/planning/"
                "?fa=downloadDocument&id=3364715&public_record_id=406569"
            ),
            intent=RequestIntent.DETAIL,
        )
        with pytest.raises(AttachmentBodyBlockedError, match="cheshireeast"):
            await session.fetch(request)
        await session.aclose()

    asyncio.run(exercise())
    assert requests == 0
    assert session.transferred_bytes == 0
    assert session.attachment_body_requests == 1


def test_evidence_store_syncs_file_then_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_modes: list[str] = []
    real_fsync = os.fsync

    def tracking_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        sync_modes.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    body = b"durable evidence"
    capture = EvidenceCapture(
        url=HttpUrl("https://example.test/source"),
        media_type="text/html",
        body=body,
        digest=EvidenceDigest(sha256(body).hexdigest()),
    )

    EvidenceStore(tmp_path / "evidence").put(capture)

    assert sync_modes == ["directory", "directory", "file", "directory"]


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
        ("site_address_x", ""),
        ("site_address_y", ""),
        ("valid_date_from", "18-08-2026"),
        ("valid_date_to", "16-09-2026"),
        ("proposal", ""),
    )

    legend_form = cheshire.parse_search_form(
        _search_form().replace(
            b'<input name="valid_date_from" value="">',
            b'<fieldset disabled><legend><fieldset><input name="valid_date_from" '
            b'value=""></fieldset></legend></fieldset>',
        )
    )
    legend_request = cheshire.valid_date_request(
        legend_form,
        DiscoveryWindow(
            start=date(2026, 8, 18),
            end=date(2026, 9, 16),
            include_open=True,
        ),
    )
    assert ("valid_date_from", "18-08-2026") in tuple(
        (field.name, field.value) for field in legend_request.form
    )


def test_cheshire_form_boundary_rejects_external_associated_controls() -> None:
    body = (
        _search_form().replace(
            b'<input type="hidden" name="fa"',
            b'<input form="form" type="hidden" name="fa"',
        )
        + b'<input form="form" name="external_token" value="x">'
    )

    with pytest.raises(cheshire.CheshireEastParseError):
        cheshire.parse_search_form(body)


def test_cheshire_successful_select_options_match_browser_disabledness() -> None:
    form = BeautifulSoup(
        """
        <form>
          <select name="kind" multiple>
            <option selected disabled value="disabled">Disabled</option>
            <optgroup disabled><option selected value="group">Group</option></optgroup>
            <option selected> Text fallback </option>
          </select>
          <select name="single">
            <option selected disabled value="disabled">Disabled</option>
            <option value="narrow">Narrow</option>
          </select>
          <input type="checkbox" name="default_checkbox" checked>
        </form>
        """,
        "html.parser",
    ).select_one("form")
    assert isinstance(form, Tag)

    fields = cheshire._successful_form_fields(form, {})

    assert tuple((field.name, field.value) for field in fields) == (
        ("kind", "Text fallback"),
        ("default_checkbox", "on"),
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
        <div class="centered application-list">
        <table id="application_results_table">
        <tr><th>Reference</th><th>Application Type</th><th>Location</th>
        <th>Proposal</th><th>View</th></tr>
        <tr><td>26/1/FUL</td><td>Full</td><td>One Road</td><td>Build</td>
        <td><button class="view_application" data-id="1">View</button></td></tr>
        </table>
        </div>
        """
    )
    assert result.explicit_zero is False
    assert result.results[0].public_reference == "26/1/FUL"

    zero = cheshire.parse_search_boundary(
        b'<div class="col-sm-12 col-md-12 animation-fadeIn application-list">'
        b'<div class="push-30-t"><strong class="text-danger">'
        b"No Results Found.</strong></div></div>"
    )
    assert zero.explicit_zero is True

    with pytest.raises(cheshire.CheshireEastParseError):
        cheshire._parse_result_table(
            b'<table id="application_results_table"><tr><th>Reference</th>'
            b"<th>Application Type</th><th>Location</th><th>Proposal</th>"
            b"<th>View</th></tr></table>"
        )

    paged = cheshire.parse_search_boundary(
        _search_results().replace(
            b"</table>",
            b'</table><nav class="pagination"><a href="?page=2">Next</a></nav>',
        )
    )
    assert paged.reported_total is None
    assert paged.pagination_links == ("?page=2",)
    with pytest.raises(cheshire.CheshireEastParseError):
        cheshire.parse_search_boundary(
            _search_results().replace(
                b'<div class="centered application-list">',
                b'<div class="centered application-list" data-result-count="1">',
            )
        )

    for body in (
        b"<main></main>",
        b"<p>No Results Found</p>",
        b'<div class="centered application-list"><script>No Results Found.</script></div>',
        b'<div class="centered application-list"><style>No Results Found.</style></div>',
        b'<div class="centered application-list"><template>No Results Found.</template></div>',
        b'<div class="centered application-list"><title>No Results Found.</title></div>',
        b'<div class="centered application-list"><noscript>No Results Found.</noscript></div>',
        b'<div class="centered application-list"><p hidden>No Results Found.</p></div>',
        b'<div class="centered application-list"><p aria-hidden="true">No Results Found.</p></div>',
        b'<div class="centered application-list"><p style="display:none">No Results Found.</p></div>',
        b'<div class="centered application-list"><p>No Results Found.</p>'
        + _search_results()
        + b"</div>",
        _search_results().replace(
            b"</table>",
            b'</table><div class="push-30-t">'
            b'<strong class="text-danger">No Results Found.</strong></div>',
            1,
        ),
        (
            b'<div class="col-sm-12 col-md-12 animation-fadeIn application-list">'
            b'<div class="push-30-t"><strong class="text-danger" hidden>'
            b"No Results Found.</strong></div></div>"
        ),
        (
            b'<main hidden><div class="col-sm-12 col-md-12 animation-fadeIn '
            b'application-list"><div class="push-30-t"><strong '
            b'class="text-danger">No Results Found.</strong></div></div></main>'
        ),
        (
            b'<main aria-hidden="true"><div class="col-sm-12 col-md-12 '
            b'animation-fadeIn application-list"><div class="push-30-t">'
            b'<strong class="text-danger">No Results Found.</strong></div>'
            b"</div></main>"
        ),
        (
            b'<main style="display:none"><div class="col-sm-12 col-md-12 '
            b'animation-fadeIn application-list"><div class="push-30-t">'
            b'<strong class="text-danger">No Results Found.</strong></div>'
            b"</div></main>"
        ),
        (
            b'<main style="visibility:hidden"><div class="col-sm-12 col-md-12 '
            b'animation-fadeIn application-list"><div class="push-30-t">'
            b'<strong class="text-danger">No Results Found.</strong></div>'
            b"</div></main>"
        ),
        (
            b'<main class="hidden"><div class="col-sm-12 col-md-12 '
            b'animation-fadeIn application-list"><div class="push-30-t">'
            b'<strong class="text-danger">No Results Found.</strong></div>'
            b"</div></main>"
        ),
        (
            b'<main class="hide"><div class="col-sm-12 col-md-12 '
            b'animation-fadeIn application-list"><div class="push-30-t">'
            b'<strong class="text-danger">No Results Found.</strong></div>'
            b"</div></main>"
        ),
        (
            b'<main class="collapse"><div class="col-sm-12 col-md-12 '
            b'animation-fadeIn application-list"><div class="push-30-t">'
            b'<strong class="text-danger">No Results Found.</strong></div>'
            b"</div></main>"
        ),
        (
            b'<dialog><div class="col-sm-12 col-md-12 animation-fadeIn '
            b'application-list"><div class="push-30-t"><strong '
            b'class="text-danger">No Results Found.</strong></div></div></dialog>'
        ),
        (
            b'<details><summary>Other</summary><div class="col-sm-12 col-md-12 '
            b'animation-fadeIn application-list"><div class="push-30-t">'
            b'<strong class="text-danger">No Results Found.</strong></div>'
            b"</div></details>"
        ),
        (
            b'<main style="display:none!important"><div class="col-sm-12 col-md-12 '
            b'animation-fadeIn application-list"><div class="push-30-t">'
            b'<strong class="text-danger">No Results Found.</strong></div>'
            b"</div></main>"
        ),
        (
            b'<main style="display:none!important;display:block"><div '
            b'class="col-sm-12 col-md-12 animation-fadeIn application-list">'
            b'<div class="push-30-t"><strong class="text-danger">'
            b"No Results Found.</strong></div></div></main>"
        ),
        (
            b'<main style="display/**/:none"><div class="col-sm-12 col-md-12 '
            b'animation-fadeIn application-list"><div class="push-30-t">'
            b'<strong class="text-danger">No Results Found.</strong></div>'
            b"</div></main>"
        ),
        (
            b'<main style="display:none/*comment*/"><div class="col-sm-12 '
            b'col-md-12 animation-fadeIn application-list"><div '
            b'class="push-30-t"><strong class="text-danger">'
            b"No Results Found.</strong></div></div></main>"
        ),
        (
            b'<main style="display:none ! important"><div class="col-sm-12 '
            b'col-md-12 animation-fadeIn application-list"><div '
            b'class="push-30-t"><strong class="text-danger">'
            b"No Results Found.</strong></div></div></main>"
        ),
        (
            b'<main style="d\\69 splay:none"><div class="col-sm-12 col-md-12 '
            b'animation-fadeIn application-list"><div class="push-30-t">'
            b'<strong class="text-danger">No Results Found.</strong></div>'
            b"</div></main>"
        ),
        (
            b'<main popover><div class="col-sm-12 col-md-12 animation-fadeIn '
            b'application-list"><div class="push-30-t"><strong '
            b'class="text-danger">No Results Found.</strong></div></div></main>'
        ),
        (
            b'<datalist><div class="col-sm-12 col-md-12 animation-fadeIn '
            b'application-list"><div class="push-30-t"><strong '
            b'class="text-danger">No Results Found.</strong></div></div></datalist>'
        ),
        (
            b'<canvas><div class="col-sm-12 col-md-12 animation-fadeIn '
            b'application-list"><div class="push-30-t"><strong '
            b'class="text-danger">No Results Found.</strong></div></div></canvas>'
        ),
        (
            b'<head><div class="col-sm-12 col-md-12 animation-fadeIn '
            b'application-list"><div class="push-30-t"><strong '
            b'class="text-danger">No Results Found.</strong></div></div></head>'
        ),
        (
            b'<noscript><div class="col-sm-12 col-md-12 animation-fadeIn '
            b'application-list"><div class="push-30-t"><strong '
            b'class="text-danger">No Results Found.</strong></div></div></noscript>'
        ),
        (
            b'<select><div class="col-sm-12 col-md-12 animation-fadeIn '
            b'application-list"><div class="push-30-t"><strong '
            b'class="text-danger">No Results Found.</strong></div></div></select>'
        ),
        _search_results().replace(
            b'<div class="centered application-list">',
            b'<div class="centered application-list" hidden>',
        ),
        _search_results().replace(b"<tr><td>26/3335", b"<tr hidden><td>26/3335"),
        _search_results().replace(
            b'<button class="view_application" data-id="406569">View</button>',
            b'<button class="view_application" data-id="406569">View</button>'
            b'<button class="view_application" data-id="9" hidden>View</button>',
        ),
        _search_results().replace(b'data-id="406569"', b'data-id="not-numeric"'),
        _search_results().replace(b"<th>Reference</th>", b"<th>Reference Notes</th>"),
        _search_results().replace(
            b"<th>Reference</th>", b'<th class="hidden">Reference</th>'
        ),
        _search_results().replace(
            b"<td>26/3335/PRIOR-1A</td>",
            b'<td class="hidden">26/3335/PRIOR-1A</td>',
        ),
        _search_results().replace(
            b"<td>26/3335/PRIOR-1A</td>",
            b"<td><span hidden>FAKE</span>26/3335/PRIOR-1A</td>",
        ),
        _search_results().replace(
            b"<td>Single storey rear extension.</td>\n      <td>"
            b'<button class="view_application" data-id="406569">View</button></td>',
            b"<td>Single storey rear extension."
            b'<button class="view_application" data-id="406569">View</button></td>'
            b"<td></td>",
        ),
        _search_results().replace(
            b"</table>",
            b'</table><nav class="pagination" hidden><a href="?page=2">Next</a></nav>',
        ),
        _search_results().replace(
            b"</table>", b'</table><span data-result-count="0"></span>'
        ),
        _search_results().replace(
            b"</table>", b'</table><span data-result-count="many"></span>'
        ),
        _search_results().replace(
            b"<tr><td>26/3335/PRIOR-1A</td>",
            b"<tr><td>26/3335/PRIOR-1A</td><td>extra</td>",
        ),
        _search_results().replace(
            b"</table>",
            b"<tr><td>26/3335/PRIOR-1A</td><td>Full</td>"
            b"<td>Elsewhere</td><td>Other</td><td><button "
            b'class="view_application" data-id="9">View</button></td></tr></table>',
        ),
        (
            b'<div class="col-sm-12 col-md-12 animation-fadeIn application-list">'
            b'<div class="push-30-t"><strong class="text-danger">'
            b'No Results Found.</strong></div><nav class="pagination">'
            b'<a href="?page=2" aria-label="Next"></a></nav></div>'
        ),
        (
            b'<div class="col-sm-12 col-md-12 animation-fadeIn application-list">'
            b'<div class="push-30-t"><strong class="text-danger">'
            b'No Results Found.</strong></div><a rel="next" href="?page=2"></a></div>'
        ),
        (
            b'<div class="col-sm-12 col-md-12 animation-fadeIn application-list">'
            b'<div class="push-30-t"><strong class="text-danger">'
            b'No Results Found.</strong></div><span data-result-count="1"></span></div>'
        ),
        (
            b'<div class="col-sm-12 col-md-12 animation-fadeIn application-list">'
            b'<div class="push-30-t"><strong class="text-danger">'
            b'No Results Found.</strong></div><button type="button" '
            b'aria-label="Next page"></button></div>'
        ),
        (
            b'<div class="col-sm-12 col-md-12 animation-fadeIn application-list">'
            b'<div class="push-30-t"><strong class="text-danger">'
            b"<span hidden>No</span> Results Found.</strong></div></div>"
        ),
        (
            b'<div class="col-sm-12 col-md-12 animation-fadeIn application-list" '
            b'data-result-count="1"><div class="push-30-t"><strong '
            b'class="text-danger">No Results Found.</strong></div></div>'
        ),
        (
            b'<div class="col-sm-12 col-md-12 animation-fadeIn application-list">'
            b'<div class="push-30-t"><strong class="text-danger">'
            b'No Results Found.<a rel="next" href="?page=2" '
            b'aria-label="Next"></a></strong></div></div>'
        ),
        (
            b'<div class="col-sm-12 col-md-12 animation-fadeIn application-list">'
            b'<div class="push-30-t"><strong class="text-danger">'
            b"<span>No</span> Results Found.</strong></div></div>"
        ),
    ):
        with pytest.raises(cheshire.CheshireEastParseError):
            cheshire.parse_search_boundary(body)

    for body in (
        _search_form().replace(
            b'action="/planning/index.html"', b'action="/planning/wrong"'
        ),
        _search_form().replace(b'action="/planning/index.html"', b'action="http://["'),
        _search_form().replace(
            b'method="post"', b'method="post" enctype="multipart/form-data"'
        ),
        _search_form().replace(
            b'<input type="hidden" name="fa"',
            b'<input form="other" type="hidden" name="fa"',
        ),
        _search_form().replace(
            b'<input type="hidden" name="fa"',
            b'<input form="" type="hidden" name="fa"',
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
        _search_form().replace(
            b'<input name="valid_date_from" value="">',
            b'<select name="valid_date_from">'
            b'<option selected value="fixed">Fixed</option></select>',
        ),
        _search_form().replace(
            b'<input name="valid_date_from" value="">',
            b'<fieldset disabled><input name="valid_date_from" value=""></fieldset>',
        ),
        _search_form().replace(b'type="hidden" name="fa"', b'type="submit" name="fa"'),
        _search_form().replace(
            b'<textarea name="proposal">House</textarea>',
            b'<textarea name="proposal">House</textarea>'
            b'<input name="proposal" value="Other">',
        ),
        _search_form().replace(
            b'<option value="" selected>Any</option>',
            b'<option value="4" selected>Full</option>',
        ),
        _search_form().replace(
            b"</form>", b'<input name="unknown_filter" value="narrow"></form>'
        ),
        _search_form().replace(
            b"</form>",
            b'<input type="hidden" name="unknown_hidden_filter" value="narrow"></form>',
        ),
        _search_form() + _search_form(),
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
    fields = cheshire._successful_form_fields(custom, {})
    assert tuple((field.name, field.value) for field in fields) == (
        ("f", "f"),
        ("g", "g"),
    )


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
    assert boundary.reported_total is None
    assert boundary.pagination_links == ("?page=2",)
    assert boundary.terminal_marker is False
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
        _weekly_form().replace(
            b'action="/planning/index.html?fa=getReceivedWeeklyList"',
            b'action="http://["',
        ),
        _weekly_form().replace(b'method="post"', b'method="post" enctype="text/plain"'),
        _weekly_form().replace(
            b'<input type="text" id="week" name="week"',
            b'<input form="other" type="text" id="week" name="week"',
        ),
        _weekly_form().replace(
            b'<input type="text" id="week" name="week"',
            b'<input form="" type="text" id="week" name="week"',
        ),
        _weekly_form().replace(b'name="week"', b'name="other"'),
        _weekly_form().replace(b'name="fa" value=""', b'name="fa" value="x"'),
        _weekly_form().replace(
            b'<input type="text" id="week" name="week" value="14-09-2026">',
            b'<fieldset disabled><input type="text" id="week" name="week" '
            b'value="14-09-2026"></fieldset>',
        ),
        _weekly_form().replace(
            b'<input type="text" id="week" name="week" value="14-09-2026">',
            b'<select id="week" name="week">'
            b'<option selected value="fixed">Fixed</option></select>',
        ),
        _weekly_form().replace(
            b'<input type="hidden" name="fa" value="">',
            b'<input type="hidden" name="extra" value="x">'
            b'<input type="hidden" name="fa" value="">',
        ),
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
        _weekly_results().replace(
            b"/planning/index.html?fa=getApplication&amp;id=400001",
            b"http://[",
        ),
        _weekly_results().replace(b"<table>", b'<table hidden data-result-count="50">'),
        _weekly_results().replace(b"<tr><td>24/0001D", b"<tr hidden><td>24/0001D"),
        _weekly_results().replace(
            b'<a href="/planning/index.html?fa=getApplication&amp;id=400001">View</a>',
            b'<a hidden href="/planning/index.html?fa=getApplication&amp;id=400001">View</a>',
        ),
        _weekly_results().replace(b"<td>24/0001D</td>", b"<td></td>"),
        _weekly_results().replace(b"24/0002D", b"24/0001D"),
        _weekly_results().replace(b"id=400002", b"id=400001"),
        _weekly_results().replace(
            b"<th>Application</th>", b'<th class="hidden">Application</th>'
        ),
        _weekly_results().replace(
            b"<td>24/0001D</td>", b'<td class="hidden">24/0001D</td>'
        ),
        _weekly_results().replace(
            b"<td>24/0001D</td>", b"<td><span hidden>24/0001D</span></td>"
        ),
        _weekly_results() + _weekly_results().replace(b"<table>", b"<table hidden>", 1),
        b'<main class="hidden">' + _weekly_results() + b"</main>",
        b'<main style="visibility:hidden!important">' + _weekly_results() + b"</main>",
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
            _detail().replace(b"374136, 360487", b"9" * 400 + b", 360487"),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b" disabled>All Documents Loaded", b">All Documents Loaded"
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(b"All Documents Loaded", b"Finished"),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b'<button id="all_documents_loaded_application_documents" disabled>'
                b"All Documents Loaded</button>",
                b'<span id="all_documents_loaded_application_documents" disabled>'
                b"All Documents Loaded</span>",
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(b">Show More</button>", b">Continue</button>"),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b'<button id="show_more_documents_application_documents" '
                b'style="display:none">Show More</button>',
                b'<div id="show_more_documents_application_documents" '
                b'style="display:none">Show More</div>',
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b" disabled>All Documents Loaded",
                b" disabled hidden>All Documents Loaded",
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(b'<div id="documents">', b'<div id="documents" hidden>'),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(b"<thead><tr>", b"<thead><tr><th>Unexpected</th>"),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b'<th data-field-name="document_type">Document Type</th>',
                b'<th class="hidden" data-field-name="document_type">'
                b"Document Type</th>",
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b'<td data-field-name="document_type">Submitted Plans</td>',
                b'<td class="hidden" data-field-name="document_type">'
                b"Submitted Plans</td>",
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b'<td data-field-name="document_type">Submitted Plans</td>',
                b'<td data-field-name="document_type"><span hidden>FAKE</span>'
                b"Submitted Plans</td>",
            ),
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
            _detail().replace(
                b"/planning/?fa=downloadDocument&amp;id=3364715&amp;public_record_id=406569",
                b"http://[",
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(b"public_record_id=406569", b"public_record_id=9"),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b'<div id="application_details"',
                b'<div hidden id="application_details"',
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b'<div class="row pad-bottom-5"><div><strong>Valid Date:',
                b'<div hidden class="row pad-bottom-5"><div><strong>Valid Date:',
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(b"<tbody><tr>", b"<tbody><tr hidden>"),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b'<a href="/planning/?fa=downloadDocument',
                b'<a hidden href="/planning/?fa=downloadDocument',
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(b'style="display:none"', b'style="display:none-block"'),
            cheshire.CheshireEastParseError,
        ),
        (
            b'<main class="hidden">' + _detail() + b"</main>",
            cheshire.CheshireEastParseError,
        ),
        (
            b'<main style="display:none !important">' + _detail() + b"</main>",
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b"<strong>Application Status:</strong>",
                b"<strong><span hidden>Wrong</span>Application Status:</strong>",
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b" disabled>All Documents Loaded</button>",
                b" disabled><span hidden>All</span> Documents Loaded</button>",
            ),
            cheshire.CheshireEastParseError,
        ),
        (
            _detail().replace(
                b'style="display:none">Show More</button>',
                b'style="display:none"><span hidden>Show</span> More</button>',
            ),
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


def test_cheshire_document_boundary_rejects_ambiguous_or_unscoped_controls() -> None:
    duplicate_section = BeautifulSoup(_detail(), "html.parser")
    section = duplicate_section.select_one("#documents")
    assert isinstance(section, Tag)
    duplicate_section.append(BeautifulSoup(str(section), "html.parser"))

    duplicate_table = BeautifulSoup(_detail(), "html.parser")
    table = duplicate_table.select_one("table#application_documents")
    table_section = duplicate_table.select_one("#documents")
    assert isinstance(table, Tag)
    assert isinstance(table_section, Tag)
    table_section.append(BeautifulSoup(str(table), "html.parser"))

    duplicate_loaded = BeautifulSoup(_detail(), "html.parser")
    loaded = duplicate_loaded.select_one("#all_documents_loaded_application_documents")
    loaded_section = duplicate_loaded.select_one("#documents")
    assert isinstance(loaded, Tag)
    assert isinstance(loaded_section, Tag)
    loaded_section.append(BeautifulSoup(str(loaded), "html.parser"))

    duplicate_show_more = BeautifulSoup(_detail(), "html.parser")
    show_more = duplicate_show_more.select_one(
        "#show_more_documents_application_documents"
    )
    show_more_section = duplicate_show_more.select_one("#documents")
    assert isinstance(show_more, Tag)
    assert isinstance(show_more_section, Tag)
    show_more_section.append(BeautifulSoup(str(show_more), "html.parser"))

    unscoped_loaded = BeautifulSoup(_detail(), "html.parser")
    outside_loaded = unscoped_loaded.select_one(
        "#all_documents_loaded_application_documents"
    )
    assert isinstance(outside_loaded, Tag)
    unscoped_loaded.append(outside_loaded.extract())

    unscoped_show_more = BeautifulSoup(_detail(), "html.parser")
    outside_show_more = unscoped_show_more.select_one(
        "#show_more_documents_application_documents"
    )
    assert isinstance(outside_show_more, Tag)
    unscoped_show_more.append(outside_show_more.extract())

    for soup in (
        duplicate_section,
        duplicate_table,
        duplicate_loaded,
        duplicate_show_more,
        unscoped_loaded,
        unscoped_show_more,
    ):
        with pytest.raises(cheshire.CheshireEastParseError):
            cheshire.parse_detail_contract(
                str(soup).encode(),
                expected_reference="26/3335/PRIOR-1A",
                expected_locator="406569",
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


def test_cheshire_resume_continues_from_the_first_incomplete_stage(
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

    class InterruptedSession(_QualificationSession):
        async def fetch(self, request: PortalRequest) -> EvidenceCapture:
            if len(self.requests) == 2:
                self.requests.append(request)
                message = "interrupted"
                raise SourceUnavailableError(message)
            return await super().fetch(request)

    first = InterruptedSession()
    assert (
        module.main(
            arguments,
            session_factory=lambda: first,
            now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
        )
        == 1
    )
    assert len(first.requests) == 3
    assert (data_dir / "cheshire-east-qualification-journal-v1.json").is_file()
    capsys.readouterr()

    resumed_session = _QualificationSession()
    assert (
        module.main(
            [*arguments, "--resume"],
            session_factory=lambda: resumed_session,
            now=lambda: datetime(2026, 9, 16, 9, 1, tzinfo=UTC),
        )
        == 1
    )
    assert [request.url for request in resumed_session.requests] == [
        cheshire.weekly_received_form_request().url,
        cheshire.weekly_received_request(
            cheshire.parse_weekly_form(_weekly_form()), date(2024, 1, 1)
        ).url,
        cheshire.detail_request("406569").url,
    ]
    capsys.readouterr()


def test_cheshire_resume_uses_only_consumed_completed_journal_evidence(
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
    assert module.main(arguments, session_factory=_QualificationSession) == 1
    capsys.readouterr()
    (data_dir / "cheshire-east-qualification-blocker-v2.json").unlink()
    journal_path = data_dir / "cheshire-east-qualification-journal-v1.json"
    journal = module.CheshireEastQualificationJournalV1.model_validate_json(
        journal_path.read_text(encoding="utf-8")
    )
    first_evidence = journal.stages[0].evidence
    assert first_evidence is not None
    drift = b"<html><body>changed source contract</body></html>"
    capture = EvidenceCapture(
        url=first_evidence.source_url,
        media_type="text/html",
        body=drift,
        digest=EvidenceDigest(sha256(drift).hexdigest()),
    )
    retained = module._retain_evidence(
        EvidenceStore(data_dir / "evidence"),
        (capture,),
    )[0]
    first = journal.stages[0].model_copy(update={"evidence": retained})
    module._write_model(
        journal_path,
        journal.model_copy(update={"stages": (first, *journal.stages[1:])}),
    )
    sessions: list[_QualificationSession] = []

    def session_factory() -> _QualificationSession:
        session = _QualificationSession()
        sessions.append(session)
        return session

    assert module.main([*arguments, "--resume"], session_factory=session_factory) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(sessions) == 1
    assert sessions[0].requests == []
    receipt = module.CheshireEastQualificationBlockerReceiptV2.model_validate_json(
        (data_dir / "cheshire-east-qualification-blocker-v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt.query_inventory == (
        "source-access|search-form",
        "recent|valid|2026-08-18|2026-09-16",
        "source-access|weekly-form",
        "older-open|weekly-received|2024-01-01",
        "detail|406569",
    )
    assert receipt.pending_query_inventory == ()
    assert receipt.decision_query_key == "source-access|search-form"
    assert len(receipt.evidence) == 5
    assert receipt.costs.request_count == 5
    assert receipt.blockers[0].code == "official-source-contract-drift"


def test_cheshire_qualification_implementation_is_covered_package_code() -> None:
    implementation = _ROOT / "src" / "yimby" / "cheshire_qualification.py"
    wrapper = (_ROOT / "scripts" / "qualify_cheshire_east.py").read_text(
        encoding="utf-8"
    )

    assert implementation.is_file()
    assert "class CheshireEastQualificationBlockerReceiptV2" not in wrapper


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


def test_cheshire_redirected_drift_becomes_a_typed_blocker_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / "qualification"

    class RedirectedDriftSession(_QualificationSession):
        async def fetch(self, request: PortalRequest) -> EvidenceCapture:
            capture = await super().fetch(request)
            body = b"<html>challenge</html>"
            return capture.model_copy(
                update={
                    "url": HttpUrl("https://pa.cheshireeast.gov.uk/planning/challenge"),
                    "body": body,
                    "digest": EvidenceDigest(sha256(body).hexdigest()),
                }
            )

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
        session_factory=RedirectedDriftSession,
        now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
    )

    assert result == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    receipt = module.CheshireEastQualificationBlockerReceiptV2.model_validate_json(
        (data_dir / "cheshire-east-qualification-blocker-v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt.decision_query_key == "source-access|search-form"
    assert str(receipt.evidence[0].source_url).endswith("/planning/challenge")
    assert receipt.blockers[0].code == "official-search-form-unavailable"


def test_cheshire_non_html_search_form_becomes_an_offline_blocker_receipt(
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
            session_factory=_NonHtmlQualificationSession,
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
    capsys.readouterr()

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
    capsys.readouterr()

    payload = json.loads(
        (data_dir / "cheshire-east-qualification-blocker-v2.json").read_text(
            encoding="utf-8"
        )
    )
    payload["evidence"][0]["media_type"] = "Text/HTML; charset=utf-8"
    (data_dir / "cheshire-east-qualification-blocker-v2.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
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

    for media_type in (
        "application/pdf",
        "Application/PDF",
        "application/pdf; charset=binary",
    ):
        payload["evidence"][0]["media_type"] = media_type
        (data_dir / "cheshire-east-qualification-blocker-v2.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        assert (
            module.main(
                [*arguments, "--resume"],
                session_factory=forbidden_factory,
                now=lambda: datetime(2026, 9, 16, 9, 2, tzinfo=UTC),
            )
            == 1
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert '"error": "runtime-failure"' in captured.err


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
        _search_form().replace(b'type="hidden" name="fa"', b'type="submit" name="fa"'),
        _search_form().replace(
            b'<textarea name="proposal">House</textarea>',
            b'<textarea name="proposal">House</textarea>'
            b'<input name="proposal" value="Other">',
        ),
    ],
    ids=(
        "method",
        "discriminator",
        "disabled-date",
        "non-successful-required",
        "duplicate-successful-name",
    ),
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

    def forbidden_factory() -> _QualificationSession:
        message = "offline resume constructed a portal session"
        raise AssertionError(message)

    assert (
        module.main(
            [
                "--confirm-live",
                "--data-dir",
                str(data_dir),
                "--start",
                "2026-08-18",
                "--end",
                "2026-09-16",
                "--include-open",
                "--resume",
            ],
            session_factory=forbidden_factory,
            now=lambda: datetime(2026, 9, 16, 9, 1, tzinfo=UTC),
        )
        == 1
    )


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
            {
                "weekly_form": _weekly_form().replace(
                    b'<input type="hidden" name="fa" value="">',
                    b'<input type="hidden" name="extra" value="x">'
                    b'<input type="hidden" name="fa" value="">',
                )
            },
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
                    b'<table hidden data-result-count="49">',
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
        (("attempted_requests", 1, "form", 9, "value"), "Tampered proposal"),
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
        (("blockers", 0, "explanation"), "qualification succeeded"),
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
        "blocker-explanation",
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


@pytest.mark.parametrize(
    ("evidence_index", "old", "new"),
    [
        (1, b"139 Abbey Road", b"999 Other Road"),
        (1, b'data-id="406569"', b'data-id="999999"'),
        (3, b"id=400001", b"id=499999"),
        (4, b"374136, 360487", b"374137, 360487"),
    ],
    ids=("recent-field", "recent-locator", "weekly-row", "detail-grid"),
)
def test_cheshire_offline_resume_binds_all_parsed_source_semantics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    evidence_index: int,
    old: bytes,
    new: bytes,
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
                search_results=_search_results()
            ),
            now=lambda: datetime(2026, 9, 16, 9, tzinfo=UTC),
        )
        == 1
    )
    capsys.readouterr()
    receipt_path = data_dir / "cheshire-east-qualification-blocker-v2.json"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    item = payload["evidence"][evidence_index]
    evidence_path = data_dir / "evidence" / item["relative_path"]
    body = gzip.decompress(evidence_path.read_bytes())
    assert old in body
    changed = body.replace(old, new, 1)
    digest = sha256(changed).hexdigest()
    relative_path = f"{digest[:2]}/{digest}.gz"
    replacement_path = data_dir / "evidence" / relative_path
    replacement_path.parent.mkdir(parents=True, exist_ok=True)
    replacement_path.write_bytes(gzip.compress(changed, mtime=0))
    item.update(
        digest=digest,
        relative_path=relative_path,
        byte_count=len(changed),
    )
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


def test_cheshire_qualification_model_guards_cover_invalid_states(
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
    assert module.main(arguments, session_factory=_QualificationSession) == 1
    capsys.readouterr()
    receipt = module.CheshireEastQualificationBlockerReceiptV2.model_validate_json(
        (data_dir / "cheshire-east-qualification-blocker-v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt.source_contract is not None

    with pytest.raises(ValidationError):
        module.QualificationScopeV1(
            start=date(2026, 8, 18),
            end=date(2026, 9, 15),
            include_open=True,
        )
    with pytest.raises(ValidationError):
        module.QualificationJournalStageV1(
            request=receipt.attempted_requests[0],
            state="prepared",
            evidence=receipt.evidence[0],
        )
    wrong_request = receipt.attempted_requests[0].model_copy(update={"key": "wrong"})
    with pytest.raises(ValidationError):
        module.CheshireEastQualificationJournalV1(
            scope=receipt.scope,
            stages=(
                module.QualificationJournalStageV1(
                    request=wrong_request,
                    state="completed",
                    evidence=receipt.evidence[0],
                ),
            ),
        )
    with pytest.raises(ValidationError):
        module.CheshireEastQualificationJournalV1(
            scope=receipt.scope,
            stages=(
                module.QualificationJournalStageV1(
                    request=receipt.attempted_requests[0],
                    state="prepared",
                ),
                module.QualificationJournalStageV1(
                    request=receipt.attempted_requests[1],
                    state="completed",
                    evidence=receipt.evidence[1],
                ),
            ),
        )

    recent_payload = receipt.source_contract.recent.model_dump()
    with pytest.raises(ValidationError):
        module.RecentContractV1.model_validate({**recent_payload, "reported_total": 1})
    positive_recent = module.RecentContractV1(
        explicit_zero=False,
        visible_references=("26/1",),
        results=(
            cheshire.CheshireEastSearchResultV1(
                public_reference="26/1",
                application_type="Full",
                location="One Road",
                proposal="Build",
                detail_locator="1",
            ),
        ),
        reported_total=1,
        pagination_links=(),
        terminal_marker=False,
    )
    with pytest.raises(ValidationError):
        module.RecentContractV1.model_validate(
            {**positive_recent.model_dump(), "reported_total": 0}
        )
    with pytest.raises(ValidationError):
        module.DetailContractV1.model_validate(
            {**receipt.source_contract.detail.model_dump(), "document_count": 0}
        )

    receipt_payload = receipt.model_dump(mode="json")
    with pytest.raises(ValidationError):
        module.CheshireEastQualificationBlockerReceiptV2.model_validate(
            {
                **receipt_payload,
                "checks": [*receipt_payload["checks"], receipt_payload["checks"][0]],
            }
        )
    with pytest.raises(ValidationError):
        module.CheshireEastQualificationBlockerReceiptV2.model_validate(
            {
                **receipt_payload,
                "source_contract": None,
                "blockers": receipt_payload["blockers"][:2],
            }
        )
    with pytest.raises(ValidationError):
        module.CheshireEastQualificationBlockerReceiptV2.model_validate(
            {**receipt_payload, "checks": receipt_payload["checks"][:-1]}
        )
    with pytest.raises(ValidationError):
        module.CheshireEastQualificationBlockerReceiptV2.model_validate(
            {**receipt_payload, "decision_query_key": "not-a-planned-query"}
        )
    with pytest.raises(ValidationError):
        module.CheshireEastQualificationBlockerReceiptV2.model_validate(
            {
                **receipt_payload,
                "decision_query_key": receipt.attempted_requests[-1].key,
            }
        )
    search_unavailable = module.QualificationBlockerV1(
        code="official-search-form-unavailable",
        explanation=module._BLOCKER_EXPLANATIONS["official-search-form-unavailable"],
    )
    with pytest.raises(ValidationError):
        module.CheshireEastQualificationBlockerReceiptV2.model_validate(
            {
                **receipt_payload,
                "decision_query_key": receipt.attempted_requests[0].key,
                "source_contract": None,
                "blockers": [search_unavailable.model_dump(mode="json")],
            }
        )

    proven_weekly = receipt.source_contract.weekly.model_copy(
        update={"reported_total": receipt.source_contract.weekly.row_count}
    )
    proven_contract = receipt.source_contract.model_copy(
        update={"weekly": proven_weekly}
    )
    codes, _, _, weekly_unproved = module._source_blocker_facts(
        receipt.scope,
        proven_contract,
    )
    assert weekly_unproved is False
    assert "weekly-list-terminality-unproven" not in codes
    probe = module._ProbeResult(
        source_contract=proven_contract,
        blocker=None,
        attempted_requests=receipt.attempted_requests,
        captures=(),
        costs=receipt.costs,
    )
    with pytest.raises(ValueError, match="journal-probe-prefix-mismatch"):
        module._merge_completed_history(
            probe,
            receipt.attempted_requests[1:],
            (),
        )
    proven_receipt = module._receipt(
        receipt.scope,
        probe,
        receipt.evidence,
        receipt.created_at,
    )
    assert "weekly-list-terminality-unproven" not in {
        blocker.code for blocker in proven_receipt.blockers
    }
    with pytest.raises(ValueError, match="typed blocker"):
        module._receipt(
            receipt.scope,
            probe.model_copy(update={"source_contract": None}),
            receipt.evidence,
            receipt.created_at,
        )


def test_cheshire_qualification_runtime_guards_cover_invalid_state(
    tmp_path: Path,
) -> None:
    module = _qualification_module()
    scope = module.QualificationScopeV1(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=True,
    )
    search = module._recorded_query(
        "source-access|search-form", cheshire.search_form_request()
    )
    recent = module._recorded_query(
        "recent|valid|2026-08-18|2026-09-16",
        cheshire.valid_date_request(
            cheshire.parse_search_form(_search_form()),
            DiscoveryWindow(
                start=scope.start,
                end=scope.end,
                include_open=True,
            ),
        ),
    )
    weekly_form = module._recorded_query(
        "source-access|weekly-form", cheshire.weekly_received_form_request()
    )
    weekly = module._recorded_query(
        "older-open|weekly-received|2024-01-01",
        cheshire.weekly_received_request(
            cheshire.parse_weekly_form(_weekly_form()), date(2024, 1, 1)
        ),
    )

    with pytest.raises(ValueError, match="request-shape-mismatch"):
        module._validate_attempted_request_shapes(
            scope,
            (search, recent.model_copy(update={"form": ()})),
        )
    with pytest.raises(ValueError, match="request-shape-mismatch"):
        module._validate_attempted_request_shapes(
            scope,
            (search, recent, weekly_form, weekly.model_copy(update={"form": ()})),
        )
    with pytest.raises(ValueError, match="request-form-duplicate"):
        module._unique_recorded_fields(
            (
                module.RecordedFieldV1(name="x", value="1"),
                module.RecordedFieldV1(name="x", value="2"),
            )
        )

    journal_path = tmp_path / "journal"
    journal_path.mkdir()
    mismatched_stage = module.QualificationJournalStageV1.model_construct(
        request=recent,
        state="prepared",
        evidence=None,
    )
    journal = module.CheshireEastQualificationJournalV1.model_construct(
        scope=scope,
        stages=(mismatched_stage,),
    )
    probe_journal = module._ProbeJournal(journal_path, journal)
    with pytest.raises(ValueError, match="journal-request-mismatch"):
        asyncio.run(
            probe_journal.capture(
                _QualificationSession(),
                "source-access|search-form",
                cheshire.search_form_request(),
            )
        )

    missing_evidence_stage = module.QualificationJournalStageV1.model_construct(
        request=search,
        state="completed",
        evidence=None,
    )
    missing_evidence_journal = (
        module.CheshireEastQualificationJournalV1.model_construct(
            scope=scope,
            stages=(missing_evidence_stage,),
        )
    )
    probe_journal = module._ProbeJournal(journal_path, missing_evidence_journal)
    with pytest.raises(ValueError, match="journal-stage-evidence-missing"):
        asyncio.run(
            probe_journal.capture(
                _QualificationSession(),
                "source-access|search-form",
                cheshire.search_form_request(),
            )
        )

    incomplete = module._ProbeJournal(
        journal_path,
        module.CheshireEastQualificationJournalV1.model_construct(
            scope=scope,
            stages=(
                module.QualificationJournalStageV1(
                    request=search,
                    state="prepared",
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="journal-stage-incomplete"):
        _ = incomplete.retained_evidence

    wrong_scope = module.QualificationScopeV1(
        start=date(2026, 8, 17),
        end=date(2026, 9, 15),
        include_open=True,
    )
    module._write_model(
        journal_path / "cheshire-east-qualification-journal-v1.json",
        module.CheshireEastQualificationJournalV1(scope=wrong_scope),
    )
    config = module._Config(data_dir=journal_path, scope=scope, resume=True)
    with pytest.raises(module._QualificationConfigError, match="invalid-window"):
        module._open_probe_journal(config)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (("--include-open",), "confirmation-required"),
        (("--confirm-live",), "include-open-required"),
        (("--confirm-live", "--include-open", "--start", "bad"), "invalid-date"),
        (
            ("--confirm-live", "--include-open", "--start", "2026-09-17"),
            "invalid-window",
        ),
        (
            ("--confirm-live", "--include-open", "--end", "2026-09-15"),
            "exact-30-day-window-required",
        ),
    ],
)
def test_cheshire_qualification_rejects_invalid_configuration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    expected: str,
) -> None:
    module = _qualification_module()
    values = {
        "--data-dir": str(tmp_path / "qualification"),
        "--start": "2026-08-18",
        "--end": "2026-09-16",
    }
    supplied = set(arguments)
    argv = list(arguments)
    for flag, value in values.items():
        if flag not in supplied:
            argv.extend((flag, value))
    assert module.main(argv, session_factory=_QualificationSession) == 2
    captured = capsys.readouterr()
    assert expected in captured.err


def test_cheshire_qualification_rejects_unsafe_data_directories(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _qualification_module()
    common = [
        "--confirm-live",
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
    ]
    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    assert module.main([*common, "--data-dir", str(file_path)]) == 2
    assert "data-dir-not-directory" in capsys.readouterr().err

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "existing").write_text("x", encoding="utf-8")
    assert module.main([*common, "--data-dir", str(nonempty)]) == 2
    assert "resume-required" in capsys.readouterr().err


@pytest.mark.parametrize("resume_flags", [(), ("--resume",)])
def test_cheshire_qualification_recovers_initial_journal_publication_crash(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    resume_flags: tuple[str, ...],
) -> None:
    module = _qualification_module()
    data_dir = tmp_path / f"qualification-{len(resume_flags)}"
    data_dir.mkdir()
    (data_dir / "qualification.lock").write_text("", encoding="utf-8")
    (data_dir / ".cheshire-east-qualification-journal-v1.json.tmp").write_text(
        "partial",
        encoding="utf-8",
    )
    arguments = [
        "--confirm-live",
        "--data-dir",
        str(data_dir),
        "--start",
        "2026-08-18",
        "--end",
        "2026-09-16",
        "--include-open",
        *resume_flags,
    ]

    assert module.main(arguments, session_factory=_UnavailableQualificationSession) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert (data_dir / "cheshire-east-qualification-journal-v1.json").is_file()
    assert (data_dir / "cheshire-east-qualification-blocker-v2.json").is_file()


def test_cheshire_replay_and_retention_defensive_guards(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
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
    assert module.main(arguments, session_factory=_QualificationSession) == 1
    capsys.readouterr()
    receipt = module.CheshireEastQualificationBlockerReceiptV2.model_validate_json(
        (data_dir / "cheshire-east-qualification-blocker-v2.json").read_text(
            encoding="utf-8"
        )
    )
    bodies = tuple(
        gzip.decompress((data_dir / "evidence" / item.relative_path).read_bytes())
        for item in receipt.evidence
    )

    evidence = list(receipt.evidence)
    evidence[0] = evidence[0].model_copy(update={"media_type": "application/xhtml+xml"})
    non_html = receipt.model_copy(update={"evidence": tuple(evidence)})
    replay = module._RetainedEvidenceReplay(non_html, bodies)
    with pytest.raises(module._QualificationEvidenceError):
        replay.parse_stage(0, lambda body: body, (ValueError,), "wrong-blocker")

    def rejected_parser(body: bytes) -> bytes:
        raise cheshire.CheshireEastParseError(body.decode(errors="ignore"))

    replay = module._RetainedEvidenceReplay(receipt, bodies)
    with pytest.raises(module._QualificationEvidenceError):
        replay.parse_stage(
            0,
            rejected_parser,
            (cheshire.CheshireEastParseError,),
            "wrong-blocker",
        )

    requests = list(receipt.attempted_requests)
    requests[1] = requests[1].model_copy(update={"form": ()})
    with pytest.raises(module._QualificationEvidenceError):
        module._verify_request_evidence_contract(
            receipt.model_copy(update={"attempted_requests": tuple(requests)}),
            bodies,
        )
    requests = list(receipt.attempted_requests)
    requests[3] = requests[3].model_copy(update={"form": ()})
    with pytest.raises(module._QualificationEvidenceError):
        module._verify_request_evidence_contract(
            receipt.model_copy(update={"attempted_requests": tuple(requests)}),
            bodies,
        )

    recent = cheshire.parse_search_boundary(bodies[1])
    weekly = cheshire.parse_weekly_boundary(bodies[3])
    detail = cheshire.parse_detail_contract(
        bodies[4],
        expected_reference="26/3335/PRIOR-1A",
        expected_locator="406569",
    )

    def invalid_contract(*_args: object) -> object:
        return module.SourceContractV1.model_validate({})

    monkeypatch.setattr(module, "_source_contract_from_boundaries", invalid_contract)
    with pytest.raises(module._QualificationEvidenceError):
        module._verify_replayed_source_contract(replay, recent, weekly, detail)

    drift_receipt = receipt.model_copy(
        update={
            "source_contract": None,
            "blockers": (
                module.QualificationBlockerV1(
                    code="official-source-contract-drift",
                    explanation=(
                        "an official response no longer matched the recorded source "
                        "contract; every completed response was retained"
                    ),
                ),
            ),
        }
    )
    drift_replay = module._RetainedEvidenceReplay(drift_receipt, bodies)
    module._verify_replayed_source_contract(drift_replay, recent, weekly, detail)

    bad_path = tmp_path / "bad.gz"
    bad_path.write_bytes(gzip.compress(b"other", mtime=0))

    class BadStore(EvidenceStore):
        def put(self, _capture: EvidenceCapture) -> Path:
            return bad_path

    capture = EvidenceCapture(
        url=HttpUrl("https://example.test/source"),
        media_type="text/html",
        body=b"expected",
        digest=EvidenceDigest(sha256(b"expected").hexdigest()),
    )
    with pytest.raises(module._QualificationEvidenceError):
        module._retain_evidence(BadStore(tmp_path), (capture,))

    outside = data_dir / "outside.gz"
    outside.write_bytes(gzip.compress(b"outside", mtime=0))
    traversal = receipt.evidence[0].model_copy(
        update={"relative_path": "../outside.gz"}
    )
    with pytest.raises(module._QualificationEvidenceError):
        module._read_retained_evidence(data_dir, traversal)
    corrupt = receipt.evidence[0].model_copy(update={"digest": "0" * 64})
    with pytest.raises(module._QualificationEvidenceError):
        module._read_retained_evidence(data_dir, corrupt)

    with pytest.raises(module._QualificationConfigError, match="receipt-required"):
        module._verify_receipt(tmp_path / "missing", receipt.scope)
    wrong_scope = module.QualificationScopeV1(
        start=date(2026, 8, 17),
        end=date(2026, 9, 15),
        include_open=True,
    )
    with pytest.raises(module._QualificationConfigError, match="invalid-window"):
        module._verify_receipt(data_dir, wrong_scope)

    session = module._default_session()
    asyncio.run(session.aclose())
