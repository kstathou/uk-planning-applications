# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: ANN401, E501

"""Dorset Council public-register contracts."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import parse_qsl

import httpx

from yimby import AuthorityId, DiscoveryWindow, pilot_registry
from yimby.domain import DurableDiscoveryBatch
from yimby.http_transport import HostRateLimiter, HttpxPortalSession

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


def _disclaimer_form() -> bytes:
    return b"""
    <form method="post" action="./disclaimer.aspx?returnURL=%2f">
      <input type="hidden" name="__EVENTTARGET" value="">
      <input type="hidden" name="__VIEWSTATE" value="disclaimer-state">
      <input type="hidden" name="tag" value="one">
      <input type="hidden" name="tag" value="two">
      <input type="submit" name="ctl00$ContentPlaceHolder1$btnAccept" value="Accept">
    </form>
    """


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


def _result_page(query: str, page: int) -> bytes:
    rows = RECEIVED if query == "received-valid" else OUTSTANDING
    page_rows = rows[(page - 1) * 10 : page * 10]
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
    next_controls = (
        f"""
        <input type="submit" name="{NEXT_BUTTON}" value=" ">
        <input type="submit"
          name="ctl00$ContentPlaceHolder1$lvResults$pager$ctl02$NextButton"
          value=" ">
        """
        if page == 1
        else ""
    )
    return f"""
    <form method="post" action="./searchresults.aspx">
      <input type="hidden" name="__EVENTTARGET" value="">
      <input type="hidden" name="__EVENTARGUMENT" value="">
      <input type="hidden" name="__VIEWSTATE" value="{query}-page-{page}">
      <input type="hidden" name="tag" value="one">
      <input type="hidden" name="tag" value="two">
      <div id="ctl00_ContentPlaceHolder1_lvResults_RadDataPager1">
        <span>Page {page} of 2</span>
        <a class="rdpCurrentPage">{page}</a>
        {next_controls}
      </div>
      {rendered_rows}
      <div id="ctl00_ContentPlaceHolder1_lvResults_pager">
        <span>Page {page} of 2</span>
        <a class="rdpCurrentPage">{page}</a>
      </div>
    </form>
    """.encode()


class _DorsetMock:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []
        self.query: str | None = None
        self.page = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        fields = tuple(parse_qsl(request.content.decode(), keep_blank_values=True))
        self.requests.append((request.method, request.url.path, fields))
        accepted = "dorset-planning=accepted" in request.headers.get("cookie", "")
        if request.method == "GET" and request.url.path == ADVANCED_PATH:
            return httpx.Response(
                200,
                content=_advanced_form() if accepted else _disclaimer_form(),
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
        if request.method == "POST" and request.url.path == ADVANCED_PATH:
            assert accepted
            names = {name for name, _value in fields}
            self.query = (
                "received-valid"
                if "ctl00$ContentPlaceHolder1$btnSearch3" in names
                else "outstanding"
            )
            self.page = 1
            return httpx.Response(200, content=_result_page(self.query, self.page))
        if request.method == "POST" and request.url.path == RESULTS_PATH:
            assert accepted
            assert self.query is not None
            assert fields[-1] == (NEXT_BUTTON, " ")
            self.page += 1
            return httpx.Response(200, content=_result_page(self.query, self.page))
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
