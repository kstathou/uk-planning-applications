# Copyright (c) 2026 Kostas Stathoulopoulos

"""Leeds live qualification behavior."""

from __future__ import annotations

import asyncio
from datetime import date
from urllib.parse import parse_qsl

import httpx
import pytest

from yimby.authorities.leeds.adapter import (
    LeedsAdapter,
    LeedsCheckpointV1,
    LeedsParseError,
)
from yimby.domain import DiscoveryBatch, DiscoveryWindow
from yimby.http_transport import HostRateLimiter, HttpxPortalSession

WINDOW = DiscoveryWindow(
    start=date(2026, 8, 18),
    end=date(2026, 9, 16),
    include_open=True,
)

CASE_TYPES = (
    ("DAG", "Agricultural Determination"),
    ("ADV", "Application to Display Adverts"),
    ("CLA", "Certificate Alternative Appropriate Dev"),
    ("CLE", "Certificate of Existing Lawful Use"),
    ("CLP", "Certificate of Proposed Lawful Use"),
    ("DEM", "Demolition Notification"),
    ("COND", "Discharge of Conditions"),
    ("EXT", "Extension of Time Period"),
    ("EDD", "Extension to Determination Date"),
    ("BNG106", "Floating S106/BNG"),
    ("FU", "Full Planning Application"),
    ("HAZ", "Hazardous Substance Consent"),
    ("DHH", "Householder Determination"),
    ("LI", "Listed Building Application"),
    ("LA", "Local Authority Application Reg 4(1)"),
    ("LATR", "Local Authority Tree Works"),
    ("S106", "Modify or Discharge S106 Agreement"),
    ("MOD", "Non Material Amendment"),
    ("N1490", "Notification of Overhead Line"),
    ("NPD", "Notification under Permitted Development"),
    ("OT", "Outline Planning Application"),
    ("DPD", "Permitted Development Determination"),
    ("PIP", "Planning Permission in Principle"),
    ("PRESME", "Pre-Application (SME Builders)"),
    ("RM", "Reserved Matters Application"),
    ("TDC", "Technical Details Consent"),
    ("DTM", "Telecommunications Determination"),
    ("TWA", "Transport and Works Act 1992"),
    ("TR", "Tree Works"),
    ("UNK", "Unknown"),
)
EXPECTED_QUERY_COUNT = 43
EXPECTED_ADVANCED_QUERY_COUNT = 33


def _weekly_form() -> bytes:
    options = "".join(
        f'<option value="{value}">{value}</option>'
        for value in (
            "17/08/2026",
            "24/08/2026",
            "31/08/2026",
            "07/09/2026",
            "14/09/2026",
        )
    )
    return f"""
    <form>
      <input type="hidden" name="_csrf" value="weekly-token">
      <select name="week">{options}</select>
      <select name="dateType"><option value="DC_Validated">Validated</option></select>
      <input type="hidden" name="searchType" value="Application">
    </form>
    """.encode()


def _options(values: tuple[tuple[str, str], ...]) -> str:
    return '<option value="">All</option>' + "".join(
        f'<option value="{value}">{label}</option>' for value, label in values
    )


def _advanced_form(*, case_types: tuple[tuple[str, str], ...] = CASE_TYPES) -> bytes:
    fields = (
        "searchCriteria.reference",
        "searchCriteria.description",
        "searchCriteria.applicantName",
        "searchCriteria.ward",
        "searchCriteria.parish",
        "searchCriteria.conservationArea",
        "searchCriteria.agent",
        "searchCriteria.caseDecision",
        "searchCriteria.developmentType",
        "searchCriteria.address",
        "date(applicationValidatedStart)",
        "date(applicationValidatedEnd)",
        "date(applicationCommitteeStart)",
        "date(applicationCommitteeEnd)",
        "date(applicationDecisionStart)",
        "date(applicationDecisionEnd)",
    )
    inputs = "".join(f'<input name="{name}" value="">' for name in fields)
    case_statuses = _options(
        (("Current", "Current"), ("Decided", "Decided"), ("Unknown", "Unknown"))
    )
    appeal_statuses = _options(
        (
            ("Appeal decided", "Appeal decided"),
            ("Appeal lodged", "Appeal lodged"),
            ("Unknown", "Unknown"),
        )
    )
    return f"""
    <form id="advancedSearchForm" method="post"
          action="advancedSearchResults.do?action=firstPage">
      <input type="hidden" name="_csrf" value="advanced-token">
      {inputs}
      <select name="searchCriteria.caseType">{_options(case_types)}</select>
      <select name="searchCriteria.caseStatus">{case_statuses}</select>
      <select name="searchCriteria.appealStatus">{appeal_statuses}</select>
      <input type="hidden" name="caseAddressType" value="Application">
      <input type="hidden" name="searchType" value="Application">
      <input type="hidden" name="tag" value="one">
      <input type="hidden" name="tag" value="two">
    </form>
    """.encode()


class _LeedsSearchMock:
    def __init__(
        self,
        *,
        case_types: tuple[tuple[str, str], ...] = CASE_TYPES,
        capped_case_type: str | None = None,
        reject_requests: bool = False,
    ) -> None:
        self.case_types = case_types
        self.capped_case_type = capped_case_type
        self.reject_requests = reject_requests
        self.requests: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.reject_requests:
            message = f"unexpected request {request.method} {request.url}"
            raise AssertionError(message)
        fields = tuple(parse_qsl(request.content.decode(), keep_blank_values=True))
        self.requests.append((request.method, request.url.path, fields))
        path = request.url.path
        action = request.url.params.get("action")
        if path.endswith("/search.do") and action == "weeklyList":
            return httpx.Response(200, content=_weekly_form())
        if path.endswith("/weeklyListResults.do"):
            return httpx.Response(200, content=b"<p>No results found</p>")
        if path.endswith("/search.do") and action == "advanced":
            return httpx.Response(
                200,
                content=_advanced_form(case_types=self.case_types),
            )
        if path.endswith("/advancedSearchResults.do"):
            values = dict(fields)
            if values.get("searchCriteria.caseType") == self.capped_case_type:
                return httpx.Response(
                    200,
                    content=(
                        b"<p>Please check the search criteria: Too many results found. "
                        b"Please enter some more parameters.</p>"
                    ),
                )
            return httpx.Response(200, content=b"<p>No results found</p>")
        message = f"unexpected request {request.method} {request.url}"
        raise AssertionError(message)


def _session(mock: _LeedsSearchMock) -> HttpxPortalSession:
    return HttpxPortalSession(
        client=httpx.AsyncClient(transport=httpx.MockTransport(mock)),
        limiter=HostRateLimiter(0),
        max_attempts=1,
    )


async def _discover(
    mock: _LeedsSearchMock,
    checkpoint: LeedsCheckpointV1 | None = None,
) -> list[DiscoveryBatch[LeedsCheckpointV1]]:
    session = _session(mock)
    try:
        return [
            batch
            async for batch in LeedsAdapter().discover(
                session,
                WINDOW,
                checkpoint,
            )
        ]
    finally:
        await session.aclose()


def _completion_key(completion: object) -> str:
    return str(getattr(completion, "key", completion))


def test_leeds_runs_weekly_and_complete_advanced_inventory() -> None:
    """The bootstrap reconciles weekly, bounded, current, and appeal searches."""
    mock = _LeedsSearchMock()

    batches = asyncio.run(_discover(mock))

    checkpoint = batches[-1].next_checkpoint
    completed = tuple(_completion_key(item) for item in checkpoint.completed_queries)
    assert len(completed) == EXPECTED_QUERY_COUNT
    assert completed[:2] == (
        "17/08/2026|DC_Validated",
        "17/08/2026|DC_Decided",
    )
    assert completed[10:12] == (
        "advanced|validated|2026-08-18|2026-09-16",
        "advanced|decision|2026-08-18|2026-09-16",
    )
    assert completed[12:42] == tuple(
        f"advanced|current|{value}" for value, _ in CASE_TYPES
    )
    assert completed[-1] == "advanced|appeal|Appeal lodged"
    assert checkpoint.live_complete
    assert batches[-1].complete

    advanced_posts = [
        fields
        for method, path, fields in mock.requests
        if method == "POST" and path.endswith("/advancedSearchResults.do")
    ]
    assert len(advanced_posts) == EXPECTED_ADVANCED_QUERY_COUNT
    assert dict(advanced_posts[0])["date(applicationValidatedStart)"] == "18/08/2026"
    assert dict(advanced_posts[1])["date(applicationDecisionEnd)"] == "16/09/2026"
    assert dict(advanced_posts[2])["searchCriteria.caseStatus"] == "Current"
    assert dict(advanced_posts[2])["searchCriteria.caseType"] == "DAG"
    assert dict(advanced_posts[-1])["searchCriteria.appealStatus"] == "Appeal lodged"
    assert advanced_posts[0][-2:] == (("tag", "one"), ("tag", "two"))
    assert all(
        dict(fields)["caseAddressType"] == "Application" for fields in advanced_posts
    )


def test_leeds_rejects_advanced_case_type_taxonomy_drift() -> None:
    """A changed exhaustive partition cannot inherit the investigated proof."""
    with pytest.raises(LeedsParseError, match="case type"):
        asyncio.run(_discover(_LeedsSearchMock(case_types=CASE_TYPES[:-1])))


def test_leeds_rejects_a_capped_case_type_partition() -> None:
    """A capped partition is neither empty nor complete."""
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(_discover(_LeedsSearchMock(capped_case_type="FU")))

    assert type(raised.value).__name__ == "LeedsSearchCapError"


def test_leeds_terminal_checkpoint_rerun_has_zero_network_io() -> None:
    """The same terminal scope returns before either form is requested."""
    first = asyncio.run(_discover(_LeedsSearchMock()))
    checkpoint = first[-1].next_checkpoint
    mock = _LeedsSearchMock(reject_requests=True)

    second = asyncio.run(_discover(mock, checkpoint))

    assert len(second) == 1
    assert second[0].complete
    assert second[0].references == ()
    assert mock.requests == []
