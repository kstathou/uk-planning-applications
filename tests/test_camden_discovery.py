# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, PLR2004

"""Camden GeneralSearch discovery and checkpoint behavior."""

from datetime import date
from urllib.parse import parse_qsl, urlsplit

import pytest
from pydantic import ValidationError

from yimby.authorities.camden import discovery
from yimby.domain import DiscoveryWindow


def _window(*, include_open: bool = True) -> DiscoveryWindow:
    return DiscoveryWindow(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=include_open,
    )


def test_camden_query_inventory_is_typed_ordered_and_scope_owned() -> None:
    scope = discovery.CamdenDiscoveryScopeV1.from_window(_window())

    inventory = discovery.camden_query_inventory(scope)

    assert [query.key for query in inventory] == [
        "date:DATE_RECEIVED:2026-08-18:2026-09-16",
        "date:DATE_VALID:2026-08-18:2026-09-16",
        "date:DATE_DECISION:2026-08-18:2026-09-16",
        "status:4:REGISTERED",
        "status:14:APPEAL LODGED",
    ]
    assert [query.model_dump(mode="json") for query in inventory[:3]] == [
        {
            "kind": "date",
            "field": field,
            "start": "2026-08-18",
            "end": "2026-09-16",
        }
        for field in ("DATE_RECEIVED", "DATE_VALID", "DATE_DECISION")
    ]
    assert [query.model_dump(mode="json") for query in inventory[3:]] == [
        {"kind": "status", "status": "4"},
        {"kind": "status", "status": "14"},
    ]
    closed_scope = discovery.CamdenDiscoveryScopeV1.from_window(
        _window(include_open=False)
    )
    assert len(discovery.camden_query_inventory(closed_scope)) == 3


def test_camden_checkpoint_migrates_only_the_exact_fixture_shape() -> None:
    checkpoint = discovery.CamdenCheckpointV1.model_validate_json(
        '{"view_state_page":"next"}'
    )

    assert isinstance(checkpoint.root, discovery.CamdenFixtureCheckpointV1)
    assert checkpoint.root.view_state_page == "next"
    with pytest.raises(ValidationError):
        discovery.CamdenCheckpointV1.model_validate(
            {"view_state_page": "next", "XMLLoc": "stale"}
        )


def test_camden_live_checkpoint_rejects_incoherent_progress() -> None:
    checkpoint = discovery.initial_live_checkpoint(_window())
    live = checkpoint.root
    assert isinstance(live, discovery.CamdenLiveCheckpointV1)
    assert isinstance(live.progress, discovery.CamdenBetweenQueriesV1)
    assert live.progress.next_query_index == 0
    assert live.query_inventory == discovery.camden_query_inventory(live.scope)

    raw = live.model_dump(mode="json")
    raw["progress"] = {"kind": "terminal"}
    with pytest.raises(ValidationError, match="terminal checkpoint"):
        discovery.CamdenCheckpointV1.model_validate(raw)

    raw = live.model_dump(mode="json")
    raw["XMLLoc"] = "stale-session-token"
    with pytest.raises(ValidationError, match="extra"):
        discovery.CamdenCheckpointV1.model_validate(raw)


def test_camden_live_checkpoint_requires_full_active_prefix_union() -> None:
    initial = discovery.initial_live_checkpoint(_window()).root
    assert isinstance(initial, discovery.CamdenLiveCheckpointV1)
    first = discovery.CamdenSeenReferenceV1(reference="2026/1/P", locator="1")
    second = discovery.CamdenSeenReferenceV1(reference="2026/2/P", locator="2")
    raw = initial.model_dump(mode="json")
    raw["seen_references"] = [first.model_dump(mode="json")]
    raw["progress"] = {
        "kind": "paging-query",
        "query_index": 0,
        "reported_count": 20,
        "next_offset": 10,
        "ordered_prefix": [
            first.model_dump(mode="json"),
            second.model_dump(mode="json"),
        ],
    }
    with pytest.raises(ValidationError, match="next offset"):
        discovery.CamdenCheckpointV1.model_validate(raw)

    raw["progress"]["ordered_prefix"] = [first.model_dump(mode="json")] * 10
    with pytest.raises(ValidationError, match="unique references"):
        discovery.CamdenCheckpointV1.model_validate(raw)


def _search_form() -> bytes:
    return b"""
    <form id="M3Form" method="post" action="GeneralSearch.aspx">
      <input type="hidden" name="__VIEWSTATE" value="state">
      <input type="hidden" name="repeated" value="one">
      <input type="hidden" name="repeated" value="two">
      <input type="text" name="txtApplicationNumber" value="old">
      <input type="text" name="txtApplicantName" value="old">
      <input type="text" name="txtAgentName" value="old">
      <input type="text" name="txtSiteAddress" value="old">
      <select name="cboStreetReferenceNumber"><option value="" selected>All</option></select>
      <input type="text" name="txtProposal" value="old">
      <select name="cboWardCode"><option value="" selected>All</option></select>
      <select name="cboApplicationTypeCode"><option value="" selected>All</option></select>
      <select name="cboDevelopmentTypeCode"><option value="" selected>All</option></select>
      <select name="cboStatusCode">
        <option value="" selected>All</option>
        <option value="14">APPEAL LODGED</option>
        <option value="4">REGISTERED</option>
      </select>
      <select name="cboSelectDateValue">
        <option value="DATE_RECEIVED" selected>Date Received</option>
        <option value="DATE_VALID">Date Validated</option>
        <option value="DATE_DECISION">Decision Date</option>
      </select>
      <input type="radio" name="rbGroup" value="rbMonth">
      <select name="cboMonths"><option value="1" selected>1</option></select>
      <input type="radio" name="rbGroup" value="rbDay">
      <select name="cboDays"><option value="1" selected>1</option></select>
      <input type="radio" name="rbGroup" value="rbRange">
      <input type="text" name="dateStart" value="old">
      <input type="text" name="dateEnd" value="old">
      <input type="radio" name="rbGroup" value="rbNotApplicable" checked>
      <input type="hidden" name="edrDateSelection" value="">
      <input type="checkbox" name="ignored-check" value="yes">
      <input type="text" name="ignored-disabled" value="yes" disabled>
      <input type="submit" name="other-submit" value="Other">
      <input type="submit" name="csbtnSearch" value="Search">
    </form>
    """


def _form_pairs(request: object) -> list[tuple[str, str]]:
    return [(field.name, field.value) for field in request.form]  # type: ignore[attr-defined]


def test_camden_form_submission_preserves_successful_controls_in_order() -> None:
    form = discovery._parse_general_search_form(_search_form())
    scope = discovery.CamdenDiscoveryScopeV1.from_window(_window())
    date_query = discovery.camden_query_inventory(scope)[1]

    date_request = discovery._search_request(form, date_query)
    date_pairs = _form_pairs(date_request)

    assert date_request.url == discovery.GENERAL_SEARCH_URL
    assert date_pairs[:3] == [
        ("__VIEWSTATE", "state"),
        ("repeated", "one"),
        ("repeated", "two"),
    ]
    assert ("cboSelectDateValue", "DATE_VALID") in date_pairs
    assert ("cboStatusCode", "") in date_pairs
    assert ("rbGroup", "rbRange") in date_pairs
    assert ("dateStart", "18-08-2026") in date_pairs
    assert ("dateEnd", "16-09-2026") in date_pairs
    assert date_pairs[-1] == ("csbtnSearch", "Search")
    assert all(name not in {"ignored-check", "ignored-disabled", "other-submit"} for name, _ in date_pairs)

    status_query = discovery.camden_query_inventory(scope)[3]
    status_pairs = _form_pairs(discovery._search_request(form, status_query))
    assert ("cboStatusCode", "4") in status_pairs
    assert ("cboSelectDateValue", "DATE_RECEIVED") in status_pairs
    assert ("rbGroup", "rbNotApplicable") in status_pairs
    assert ("dateStart", "") in status_pairs
    assert ("dateEnd", "") in status_pairs


def _results_page(
    *,
    offset: int,
    total: int,
    rows: int,
    next_offset: int | None,
    duplicate_next: bool = False,
) -> bytes:
    first = offset + 1
    last = offset + rows
    marker = f"Records {first} to {last} of {total}" if rows > 1 else f"Record {first} of {total}"
    records = "".join(
        f"""
        <tr class="Row1">
          <td title="View Application Details">
            <a href="StdDetails.aspx?PARAM0=\n {1000 + offset + index} &amp;PUBLIC=Y">2026/{offset + index + 1}/P</a>
          </td>
          <td>Address</td>
        </tr>
        """
        for index in range(rows)
    )
    pager = ""
    if next_offset is not None:
        href = f"StdResults.aspx?PT=Planning&amp;PS=10&amp;XMLLoc=fresh-token&amp;p={next_offset}"
        pager = f'<a class="next" href="{href}">Next</a>'
        if duplicate_next:
            pager += f'<a class="next-bottom" href="{href}">Next</a>'
    return f"""
    <span id="lblPagePosition">{marker}</span>
    {pager}
    <table summary="Results of the Search">
      <tr><th>Application Number</th><th>Site Address</th></tr>
      {records}
    </table>
    """.encode()


def test_camden_result_pages_reconcile_counts_rows_and_pager() -> None:
    first = discovery._parse_result_page(
        _results_page(
            offset=0,
            total=12,
            rows=10,
            next_offset=10,
            duplicate_next=True,
        ),
        requested_offset=0,
    )
    assert first.reported_count == 12
    assert len(first.references) == 10
    assert first.references[0] == discovery.CamdenSeenReferenceV1(
        reference="2026/1/P", locator="1000"
    )
    assert first.next_url is not None
    assert dict(parse_qsl(urlsplit(str(first.next_url)).query))["p"] == "10"
    assert dict(parse_qsl(urlsplit(str(first.next_url)).query))["XMLLoc"] == "fresh-token"

    last = discovery._parse_result_page(
        _results_page(offset=10, total=12, rows=2, next_offset=None),
        requested_offset=10,
    )
    assert len(last.references) == 2
    assert last.next_url is None
    assert discovery._parse_result_page(
        b"No Records Found. Please resubmit search with different criteria.",
        requested_offset=0,
    ).reported_count == 0


@pytest.mark.parametrize(
    "body, offset, match",
    [
        (
            _results_page(offset=0, total=11, rows=9, next_offset=9),
            0,
            "page range",
        ),
        (
            _results_page(offset=10, total=12, rows=2, next_offset=20),
            10,
            "terminal pager",
        ),
        (
            b'<span id="lblPagePosition">Records 1 to 10 of 12</span>',
            0,
            "result table",
        ),
    ],
)
def test_camden_result_pages_fail_closed(
    body: bytes,
    offset: int,
    match: str,
) -> None:
    with pytest.raises(discovery.CamdenDiscoveryParseError, match=match):
        discovery._parse_result_page(body, requested_offset=offset)
