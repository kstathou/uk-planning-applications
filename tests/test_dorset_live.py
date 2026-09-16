# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: ANN401, E501, PLR2004

"""Dorset Council public-register contracts."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl

import httpx
import pytest

from yimby import AuthorityId, DiscoveryWindow, pilot_registry
from yimby.domain import DurableDiscoveryBatch, SourceId, SourceReference
from yimby.http_transport import HostRateLimiter, HttpxPortalSession

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from types import ModuleType

WINDOW = DiscoveryWindow(
    start=date(2026, 8, 18),
    end=date(2026, 9, 16),
    include_open=True,
)
BASE_URL = "https://planning.dorsetcouncil.gov.uk"
ADVANCED_PATH = "/advsearch.aspx"
RESULTS_PATH = "/searchresults.aspx"
DISCLAIMER_PATH = "/disclaimer.aspx"
NEXT_BUTTON = "ctl00$ContentPlaceHolder1$lvResults$RadDataPager1$ctl02$NextButton"


@dataclass(frozen=True, slots=True)
class _Result:
    reference: str
    recno: int


RECEIVED = tuple(
    _Result(f"P/FUL/2026/{index:05d}", 430_000 + index) for index in range(1, 12)
)
OUTSTANDING = (
    RECEIVED[0],
    *(_Result(f"P/OUT/2025/{index:05d}", 420_000 + index) for index in range(1, 11)),
)


def _disclaimer_form(return_url: str = "%2f") -> bytes:
    return f"""
    <form method="post" action="./disclaimer.aspx?returnURL={return_url}">
      <input type="hidden" name="__EVENTTARGET" value="">
      <input type="hidden" name="__VIEWSTATE" value="disclaimer-state">
      <input type="hidden" name="tag" value="one">
      <input type="hidden" name="tag" value="two">
      <input type="submit" name="ctl00$ContentPlaceHolder1$btnAccept" value="Accept">
    </form>
    """.encode()


def _advanced_form() -> bytes:
    return b"""
    <form method="post" action="./advsearch.aspx">
      <input type="hidden" name="__EVENTTARGET" value="">
      <input type="hidden" name="__EVENTARGUMENT" value="">
      <input type="hidden" name="__VIEWSTATE" value="advanced-state">
      <input type="hidden" name="tag" value="one">
      <input type="hidden" name="tag" value="two">
      <input type="text" name="ctl00$ContentPlaceHolder1$txtDateReceivedFrom" value="">
      <input type="text" name="ctl00$ContentPlaceHolder1$txtDateReceivedFrom$dateInput" value="">
      <input type="text" name="ctl00$ContentPlaceHolder1$txtDateReceivedTo" value="">
      <input type="text" name="ctl00$ContentPlaceHolder1$txtDateReceivedTo$dateInput" value="">
      <input type="checkbox" name="ctl00$ContentPlaceHolder1$chkOutstanding" value="on">
      <input type="submit" name="ctl00$ContentPlaceHolder1$btnSearch2" value="Search">
      <input type="submit" name="ctl00$ContentPlaceHolder1$btnSearch3" value="Search">
    </form>
    """


def _result_page(query: str, page: int, fault: str | None = None) -> bytes:
    rows = RECEIVED if query == "received-valid" else OUTSTANDING
    page_rows = list(rows[(page - 1) * 10 : page * 10])
    if fault == "duplicate-reference" and query == "received-valid" and page == 1:
        page_rows[-1] = page_rows[0]
    if fault == "duplicate-locator" and query == "received-valid" and page == 1:
        page_rows[-1] = _Result(page_rows[-1].reference, page_rows[0].recno)
    if fault == "underfull" and query == "received-valid" and page == 1:
        page_rows.pop()
    rendered_rows = "".join(
        f"""
        <div class="result">
          <a id="ctl00_ContentPlaceHolder1_lvResults_ctrl{index}_hypDisplayRecord"
             href="plandisp.aspx?recno={row.recno}">{row.reference}</a>
          <a id="ctl00_ContentPlaceHolder1_lvResults_ctrl{index}_hypDisplayRecord2"
             href="plandisp.aspx?recno={row.recno}">View this application</a>
        </div>
        """
        for index, row in enumerate(page_rows)
    )
    has_next = page == 1 or fault == "terminal-next"
    next_controls = (
        f"""
        <input type="submit" name="{NEXT_BUTTON}" value=" ">
        <input type="submit"
          name="ctl00$ContentPlaceHolder1$lvResults$pager$ctl02$NextButton"
          value=" ">
        """
        if has_next
        else ""
    )
    bottom_page = page + 1 if fault == "pager-mismatch" else page
    viewstate = "" if fault == "empty-viewstate" else f"{query}-page-{page}"
    return f"""
    <form method="post" action="./searchresults.aspx">
      <input type="hidden" name="__EVENTTARGET" value="">
      <input type="hidden" name="__EVENTARGUMENT" value="">
      <input type="hidden" name="__VIEWSTATE" value="{viewstate}">
      <input type="hidden" name="tag" value="one">
      <input type="hidden" name="tag" value="two">
      <div id="ctl00_ContentPlaceHolder1_lvResults_RadDataPager1">
        <span>Page {page} of 2</span>
        <a class="rdpCurrentPage">{page}</a>
        {next_controls}
      </div>
      {rendered_rows}
      <div id="ctl00_ContentPlaceHolder1_lvResults_pager">
        <span>Page {bottom_page} of 2</span>
        <a class="rdpCurrentPage">{bottom_page}</a>
      </div>
    </form>
    """.encode()


def _detail_page(reference: str, *, document_count: int = 2) -> bytes:
    rows = "".join(
        f"""
        <tr id="ctl00_ContentPlaceHolder1_DocumentsGrid_ctl00__{index}">
          <td></td>
          <td><a id="document-{index}" href="#"
            onclick="return RowClicked({index}); ">{published} - {title}</a>
            ({size})</td>
        </tr>
        """
        for index, published, title, size in (
            (0, "20/08/2026", "Application Form - Without Personal Data", "620kb"),
            (1, "21/08/2026", "Location Plan", "1mb"),
        )[:document_count]
    )
    return f"""
    <div id="ctl00_ContentPlaceHolder1_pvDetails">
      <span class="applabel">Application No</span><p class="appdata">{reference}</p>
      <span class="applabel">Status</span><p class="appdata">Out To Consultation</p>
      <span class="applabel">Type</span><p class="appdata">Full Planning Application</p>
      <span class="applabel">Proposal</span><p class="appdata">Build two homes &amp; plant four trees</p>
      <span class="applabel">Valid Date</span><p class="appdata">15/09/2026</p>
      <span class="applabel">Decision</span><p class="appdata"></p>
      <span class="applabel">Authority</span><p class="appdata"></p>
    </div>
    <div id="ctl00_ContentPlaceHolder1_pvLocation">
      <span class="applabel">Address</span><p class="appdata">1 High Street, Dorset</p>
      <span class="applabel">Easting</span><p class="appdata">406008</p>
      <span class="applabel">Northing</span><p class="appdata">100386</p>
      <span class="applabel">Ward</span><p class="appdata">Ferndown North Ward</p>
      <span class="applabel">Parish</span><p class="appdata">Ferndown Town</p>
    </div>
    <table id="ctl00_ContentPlaceHolder1_DocumentsGrid_ctl00">
      <thead><tr><th>Document</th><th>Size</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <script>
      var grid = {{"_gridTableViewsData":"[{{\\\"PageCount\\\":1,\\\"AllowPaging\\\":false,\\\"VirtualItemCount\\\":{document_count}}}]"}};
      function RowClicked(index) {{ return index; }}
    </script>
    """.encode()


class _DorsetMock:
    def __init__(self, *, fault: str | None = None) -> None:
        self.requests: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []
        self.query: str | None = None
        self.page = 0
        self.fault = fault

    def __call__(self, request: httpx.Request) -> httpx.Response:
        fields = tuple(parse_qsl(request.content.decode(), keep_blank_values=True))
        self.requests.append((request.method, request.url.path, fields))
        accepted = "dorset-planning=accepted" in request.headers.get("cookie", "")
        if request.method == "GET" and request.url.path == ADVANCED_PATH:
            return httpx.Response(
                200,
                content=(
                    _advanced_form()
                    if accepted
                    else _disclaimer_form(
                        "%2fadvsearch.aspx%3fAspxAutoDetectCookieSupport%3d1"
                    )
                ),
            )
        if request.method == "POST" and request.url.path == DISCLAIMER_PATH:
            assert fields == (
                ("__EVENTTARGET", ""),
                ("__VIEWSTATE", "disclaimer-state"),
                ("tag", "one"),
                ("tag", "two"),
                ("ctl00$ContentPlaceHolder1$btnAccept", "Accept"),
            )
            return httpx.Response(
                200,
                content=b"accepted",
                headers={"set-cookie": "dorset-planning=accepted; Path=/"},
            )
        if request.method == "GET" and request.url.path == "/plandisp.aspx":
            if not accepted:
                return httpx.Response(
                    200,
                    content=_disclaimer_form(
                        "%2fplandisp.aspx%3frecno%3d430001"
                        "%26AspxAutoDetectCookieSupport%3d1"
                    ),
                )
            recno = int(request.url.params["recno"])
            reference = next(
                row.reference for row in (*RECEIVED, *OUTSTANDING) if row.recno == recno
            )
            count = 1 if self.fault == "document-count" else 2
            body = _detail_page(reference, document_count=count)
            if self.fault == "document-count":
                body = body.replace(
                    b'\\"VirtualItemCount\\":1',
                    b'\\"VirtualItemCount\\":2',
                )
            return httpx.Response(
                200,
                content=body,
            )
        if request.method == "POST" and request.url.path == ADVANCED_PATH:
            assert accepted
            names = {name for name, _value in fields}
            self.query = (
                "received-valid"
                if "ctl00$ContentPlaceHolder1$btnSearch3" in names
                else "outstanding"
            )
            self.page = 1
            return httpx.Response(
                200,
                content=_result_page(self.query, self.page, self.fault),
            )
        if request.method == "POST" and request.url.path == RESULTS_PATH:
            assert accepted
            assert self.query is not None
            assert fields[-1] == (NEXT_BUTTON, " ")
            self.page += 1
            return httpx.Response(
                200,
                content=_result_page(self.query, self.page, self.fault),
            )
        message = f"unexpected request {request.method} {request.url}"
        raise AssertionError(message)


def _session(mock: Any) -> HttpxPortalSession:
    return HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(mock)),
        limiter=HostRateLimiter(0),
        max_attempts=1,
    )


def _pairs(mock: _DorsetMock, path: str) -> list[tuple[tuple[str, str], ...]]:
    return [
        fields
        for method, request_path, fields in mock.requests
        if method == "POST" and request_path == path
    ]


def _qualification_module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "qualify_dorset.py"
    name = "_test_qualify_dorset"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_dorset_live_discovery_replays_exact_forms_and_exhausts_both_queries() -> None:
    """The public package proves both captured searches and every advertised page."""
    package = pilot_registry().get(AuthorityId("dorset"))
    mock = _DorsetMock()
    session = _session(mock)

    async def discover_all() -> list[DurableDiscoveryBatch]:
        batches = [batch async for batch in package.discover(session, WINDOW, None)]
        await session.aclose()
        return batches

    batches = asyncio.run(discover_all())

    assert [len(batch.references) for batch in batches] == [10, 1, 9, 1]
    assert batches[-1].complete
    assert [
        (reference.reference, reference.locator)
        for batch in batches
        for reference in batch.references
    ] == [(row.reference, str(row.recno)) for row in (*RECEIVED, *OUTSTANDING[1:])]
    assert _pairs(mock, DISCLAIMER_PATH) == [
        (
            ("__EVENTTARGET", ""),
            ("__VIEWSTATE", "disclaimer-state"),
            ("tag", "one"),
            ("tag", "two"),
            ("ctl00$ContentPlaceHolder1$btnAccept", "Accept"),
        )
    ]
    assert _pairs(mock, ADVANCED_PATH) == [
        (
            ("__EVENTTARGET", ""),
            ("__EVENTARGUMENT", ""),
            ("__VIEWSTATE", "advanced-state"),
            ("tag", "one"),
            ("tag", "two"),
            ("ctl00$ContentPlaceHolder1$txtDateReceivedFrom", "2026-08-18"),
            (
                "ctl00$ContentPlaceHolder1$txtDateReceivedFrom$dateInput",
                "18/08/2026",
            ),
            ("ctl00$ContentPlaceHolder1$txtDateReceivedTo", "2026-09-16"),
            (
                "ctl00$ContentPlaceHolder1$txtDateReceivedTo$dateInput",
                "16/09/2026",
            ),
            ("ctl00$ContentPlaceHolder1$btnSearch3", "Search"),
        ),
        (
            ("__EVENTTARGET", ""),
            ("__EVENTARGUMENT", ""),
            ("__VIEWSTATE", "advanced-state"),
            ("tag", "one"),
            ("tag", "two"),
            ("ctl00$ContentPlaceHolder1$txtDateReceivedFrom", ""),
            ("ctl00$ContentPlaceHolder1$txtDateReceivedFrom$dateInput", ""),
            ("ctl00$ContentPlaceHolder1$txtDateReceivedTo", ""),
            ("ctl00$ContentPlaceHolder1$txtDateReceivedTo$dateInput", ""),
            ("ctl00$ContentPlaceHolder1$chkOutstanding", "on"),
            ("ctl00$ContentPlaceHolder1$btnSearch2", "Search"),
        ),
    ]
    assert _pairs(mock, RESULTS_PATH) == [
        (
            ("__EVENTTARGET", ""),
            ("__EVENTARGUMENT", ""),
            ("__VIEWSTATE", "received-valid-page-1"),
            ("tag", "one"),
            ("tag", "two"),
            (NEXT_BUTTON, " "),
        ),
        (
            ("__EVENTTARGET", ""),
            ("__EVENTARGUMENT", ""),
            ("__VIEWSTATE", "outstanding-page-1"),
            ("tag", "one"),
            ("tag", "two"),
            (NEXT_BUTTON, " "),
        ),
    ]

    checkpoint = json.loads(batches[-1].next_checkpoint.payload_json)
    assert checkpoint["live_complete"] is True
    assert checkpoint["completed_queries"] == ["received-valid", "outstanding"]
    assert checkpoint["active_query"] is None
    assert checkpoint["live_scope"] == {
        "start": "2026-08-18",
        "end": "2026-09-16",
        "include_open": True,
    }
    assert checkpoint["seen_references"] == [
        {
            "source_id": "dorset-planning-register",
            "reference": row.reference,
            "locator": str(row.recno),
        }
        for row in (*RECEIVED, *OUTSTANDING[1:])
    ]

    terminal_mock = _DorsetMock()
    terminal_session = _session(terminal_mock)

    async def repeat_terminal() -> list[DurableDiscoveryBatch]:
        result = [
            batch
            async for batch in package.discover(
                terminal_session,
                WINDOW,
                batches[-1].next_checkpoint,
            )
        ]
        await terminal_session.aclose()
        return result

    assert asyncio.run(repeat_terminal()) == [
        DurableDiscoveryBatch(
            references=(),
            next_checkpoint=batches[-1].next_checkpoint,
            complete=True,
        )
    ]
    assert terminal_mock.requests == []


def test_dorset_live_detail_accepts_disclaimer_and_retains_document_metadata() -> None:
    """A queued detail can establish consent without invoking a document action."""
    package = pilot_registry().get(AuthorityId("dorset"))
    mock = _DorsetMock()
    session = _session(mock)
    reference = SourceReference(
        source_id=SourceId("dorset-planning-register"),
        reference=RECEIVED[0].reference,
        locator=str(RECEIVED[0].recno),
    )

    async def collect_detail() -> Any:
        collected = await package.collect(session, reference)
        await session.aclose()
        return collected

    collected = asyncio.run(collect_detail())

    assert collected.normalised.reference == reference
    assert collected.normalised.proposal == "Build two homes & plant four trees"
    assert collected.normalised.status == "out-to-consultation"
    assert collected.normalised.metadata.application_type == "Full Planning Application"
    assert collected.normalised.metadata.decision is None
    assert collected.normalised.metadata.address == "1 High Street, Dorset"
    assert collected.normalised.metadata.validated_date == date(2026, 9, 15)
    assert collected.normalised.metadata.location is not None
    assert collected.normalised.metadata.location.bng_easting == 406_008
    assert collected.normalised.metadata.location.bng_northing == 100_386
    assert [document.title for document in collected.normalised.documents] == [
        "20/08/2026 - Application Form - Without Personal Data (620kb)",
        "21/08/2026 - Location Plan (1mb)",
    ]
    assert {str(document.url) for document in collected.normalised.documents} == {
        f"{BASE_URL}/plandisp.aspx?recno={RECEIVED[0].recno}#"
    }
    assert not {
        str(document.url) for document in collected.normalised.documents
    }.intersection(session.requested_urls)
    assert collected.normalised.completeness.application.kind == "complete"
    assert collected.normalised.completeness.documents.kind == "complete"
    assert collected.normalised.completeness.documents.item_count == 2
    assert collected.normalised.completeness.comments.kind == "unavailable"
    assert "public comment text" in collected.normalised.completeness.comments.reason
    assert "Application Form - Without Personal Data" in collected.native_json
    assert len(collected.evidence) == 1
    assert str(collected.evidence[0].url) == (
        f"{BASE_URL}/plandisp.aspx?recno={RECEIVED[0].recno}"
    )
    assert session.attachment_body_requests == 0
    assert all(
        "DocumentsGrid" not in dict(fields)
        for method, _path, fields in mock.requests
        if method == "POST"
    )


def test_dorset_live_resume_replays_committed_page_after_detail_consent() -> None:
    """A fresh session collects queued detail, then replays committed page proof."""
    package = pilot_registry().get(AuthorityId("dorset"))
    first_session = _session(_DorsetMock())

    async def first_page() -> DurableDiscoveryBatch:
        discovery = cast(
            "AsyncGenerator[DurableDiscoveryBatch]",
            package.discover(first_session, WINDOW, None),
        )
        try:
            return await anext(discovery)
        finally:
            await discovery.aclose()
            await first_session.aclose()

    first = asyncio.run(first_page())
    resume_mock = _DorsetMock()
    resume_session = _session(resume_mock)

    async def resume_after_detail() -> list[DurableDiscoveryBatch]:
        await package.collect(resume_session, first.references[0])
        batches = [
            batch
            async for batch in package.discover(
                resume_session,
                WINDOW,
                first.next_checkpoint,
            )
        ]
        await resume_session.aclose()
        return batches

    resumed = asyncio.run(resume_after_detail())

    assert [len(batch.references) for batch in resumed] == [1, 9, 1]
    assert resumed[-1].complete
    assert len(_pairs(resume_mock, DISCLAIMER_PATH)) == 1
    assert len(_pairs(resume_mock, RESULTS_PATH)) == 2


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("duplicate-reference", "duplicate result"),
        ("duplicate-locator", "duplicate result"),
        ("underfull", "nonterminal result rows"),
        ("pager-mismatch", "page markers"),
        ("empty-viewstate", "result form viewstate"),
        ("terminal-next", "next page"),
    ],
)
def test_dorset_live_discovery_fails_closed(fault: str, message: str) -> None:
    """Malformed page evidence never advances the durable checkpoint."""
    package = pilot_registry().get(AuthorityId("dorset"))
    session = _session(_DorsetMock(fault=fault))

    async def discover_all() -> None:
        with pytest.raises(ValueError, match=message):
            async for _batch in package.discover(session, WINDOW, None):
                pass
        await session.aclose()

    asyncio.run(discover_all())


def test_dorset_live_detail_rejects_incomplete_document_grid() -> None:
    """Document-grid count disagreement is not represented as completeness."""
    package = pilot_registry().get(AuthorityId("dorset"))
    session = _session(_DorsetMock(fault="document-count"))
    reference = SourceReference(
        source_id=SourceId("dorset-planning-register"),
        reference=RECEIVED[0].reference,
        locator=str(RECEIVED[0].recno),
    )

    async def collect_detail() -> None:
        with pytest.raises(ValueError, match="document grid"):
            await package.collect(session, reference)
        await session.aclose()

    asyncio.run(collect_detail())


def test_dorset_qualification_persists_exact_terminal_receipt(tmp_path: Path) -> None:
    """The dated command proves durable identity, evidence, and zero-fetch replay."""
    module = _qualification_module()
    mocks: list[_DorsetMock] = []

    def session_factory() -> HttpxPortalSession:
        mock = _DorsetMock()
        mocks.append(mock)
        return _session(mock)

    fixed_now = datetime(2026, 9, 16, 10, 30, tzinfo=UTC)
    exit_code = module.main(
        [
            "--confirm-live",
            "--include-open",
            "--data-dir",
            str(tmp_path),
        ],
        session_factory=session_factory,
        now=lambda: fixed_now,
    )

    assert exit_code == 0
    receipt_path = tmp_path / "dorset-qualification-v1.json"
    receipt = module.DorsetQualificationReceiptV1.model_validate_json(
        receipt_path.read_text()
    )
    assert receipt.created_at == fixed_now
    assert receipt.scope.model_dump(mode="json") == {
        "start": "2026-08-18",
        "end": "2026-09-16",
        "include_open": True,
    }
    assert receipt.query_inventory == ("received-valid", "outstanding")
    assert receipt.terminal_checkpoint.live_complete
    assert receipt.terminal_checkpoint.completed_queries == receipt.query_inventory
    assert receipt.terminal_checkpoint.active_query is None
    assert receipt.terminal_checkpoint.next_page == 1
    assert receipt.terminal_checkpoint.total_pages is None
    assert receipt.terminal_checkpoint.active_references == ()
    assert len(receipt.terminal_checkpoint.seen_references) == 21
    assert receipt.reference_agreement.count == 21
    assert (
        len(
            {
                receipt.reference_agreement.checkpoint_sha256,
                receipt.reference_agreement.durable_queue_sha256,
                receipt.reference_agreement.applications_sha256,
            }
        )
        == 1
    )
    assert receipt.counts.model_dump() == {
        "applications": 21,
        "discovered_references": 21,
        "native_versions": 21,
        "application_versions": 21,
        "document_versions": 21,
        "comment_versions": 0,
        "pending_retries": 0,
        "failed_sections": 0,
        "unmapped_records": 0,
    }
    assert receipt.costs.initial.fetch_calls == 29
    assert receipt.costs.initial.successful_requests == 29
    assert receipt.costs.initial.transferred_bytes > 0
    assert receipt.costs.initial.attachment_body_requests == 0
    assert receipt.costs.rerun.model_dump() == {
        "fetch_calls": 0,
        "successful_requests": 0,
        "transferred_bytes": 0,
        "attachment_body_requests": 0,
    }
    assert receipt.evidence.records == 21
    assert receipt.evidence.unique_digests == 21
    assert receipt.evidence.decompressed == 21
    assert receipt.evidence.digest_matches == 21
    assert receipt.evidence.failed_digests == ()
    assert receipt.run_statuses == ("succeeded", "succeeded")
    assert all(check.ok for check in receipt.checks)
    assert [cycle.model_dump(mode="json") for cycle in receipt.weekly_cycles] == [
        {"sequence": 1, "due_on": "2026-09-23", "status": "pending"},
        {"sequence": 2, "due_on": "2026-09-30", "status": "pending"},
    ]
    assert receipt.readiness == "discovery-only"
    assert receipt.http_max_attempts == 1
    assert len(mocks) == 2
    assert mocks[0].requests
    assert mocks[1].requests == []
    assert not list(tmp_path.glob(".*.tmp"))


def test_dorset_qualification_fails_closed_on_corrupt_evidence(tmp_path: Path) -> None:
    """A terminal checkpoint cannot hide a corrupt retained gzip member."""
    module = _qualification_module()

    def session_factory() -> HttpxPortalSession:
        return _session(_DorsetMock())

    arguments = [
        "--confirm-live",
        "--include-open",
        "--data-dir",
        str(tmp_path),
    ]
    assert module.main(arguments, session_factory=session_factory) == 0
    receipt_path = tmp_path / "dorset-qualification-v1.json"
    receipt_path.unlink()
    evidence_path = next((tmp_path / "evidence").rglob("*.gz"))
    evidence_path.write_bytes(b"not-gzip")

    assert (
        module.main(
            [*arguments, "--resume"],
            session_factory=session_factory,
        )
        == 1
    )
    assert not receipt_path.exists()


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ((), "confirmation-required"),
        (("--confirm-live",), "include-open-required"),
    ],
)
def test_dorset_qualification_requires_explicit_live_scope(
    arguments: tuple[str, ...],
    error: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The live command cannot silently weaken confirmation or open discovery."""
    module = _qualification_module()
    exit_code = module.main([*arguments, "--data-dir", str(tmp_path)])

    assert exit_code == 2
    assert json.loads(capsys.readouterr().err) == {"error": error}
