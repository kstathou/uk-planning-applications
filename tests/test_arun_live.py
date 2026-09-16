# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, PLR2004, SLF001

"""Arun live discovery and qualification contracts."""

from datetime import date, timedelta
from itertools import pairwise

import pytest

import yimby.authorities.arun.adapter as arun
from yimby.transport import RequestMethod


def _search_form() -> bytes:
    return b"""
    <form method="post" name="OcellaPlanningSearch" action="planningSearch">
      <input name="reference" value="stale">
      <input name="location" value="stale">
      <input name="OcellaPlanningSearch.postcode" value="stale">
      <select name="area"><option value=""></option>
        <option value="BR">BOGNOR REGIS</option></select>
      <input name="applicant" value="stale">
      <input name="agent" value="stale">
      <input type="checkbox" name="undecided" value="Y">
      <select name="type"><option value=""></option>
        <option value="PL">Planning Application</option></select>
      <input name="receivedFrom" value="">
      <input name="receivedTo" value="">
      <input name="decidedFrom" value="">
      <input name="decidedTo" value="">
      <input type="submit" name="action" value="Search">
      <input type="submit" name="action" value="Reset">
    </form>
    """


def _partial_results(query_fields: str) -> bytes:
    return f"""
    <table><tr><th>Reference</th></tr>
      <tr><td><a href="planningDetails?reference=BR/1/26/PL&amp;from=planningSearch">
        BR/1/26/PL</a></td></tr>
    </table>
    <strong>First 20 results shown, there are 2 in total</strong>
    <form method="post" action="planningSearch">
      <input type="hidden" name="action" value="Search">
      <input type="hidden" name="showall" value="showall">
      {query_fields}
      <input type="submit" value="Show all results">
    </form>
    """.encode()


def test_arun_canonical_query_plan_is_complete_and_non_overlapping() -> None:
    scope = arun.ArunDiscoveryScope(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=True,
    )

    plan = arun._canonical_query_plan(scope)

    assert len(plan) == 60
    assert isinstance(plan[0], arun.ArunReceivedQuery)
    assert plan[0].key == "received|2026-08-18|2026-09-16"
    assert isinstance(plan[1], arun.ArunDecidedQuery)
    assert plan[1].key == "decided|2026-08-18|2026-09-16"

    open_queries = plan[2:]
    assert all(isinstance(query, arun.ArunOpenReceivedQuery) for query in open_queries)
    assert open_queries[0].key == "open-received|1948-01-01|1999-12-31"
    assert open_queries[1].key == "open-received|2000-01-01|2000-12-31"
    assert open_queries[24].key == "open-received|2023-01-01|2023-12-31"
    assert open_queries[25].key == "open-received|2024-01-01|2024-01-31"
    assert open_queries[-1].key == "open-received|2026-09-01|2026-09-16"
    assert all(
        left.end + timedelta(days=1) == right.start
        for left, right in pairwise(open_queries)
    )


def test_arun_plan_without_open_contains_only_the_bounded_first_pass() -> None:
    scope = arun.ArunDiscoveryScope(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=False,
    )

    plan = arun._canonical_query_plan(scope)

    assert [query.kind for query in plan] == ["received", "decided"]


def test_arun_search_requests_replay_the_exact_portal_controls() -> None:
    form = arun._parse_search_form(_search_form())
    queries = (
        arun.ArunReceivedQuery(start=date(2026, 8, 18), end=date(2026, 9, 16)),
        arun.ArunDecidedQuery(start=date(2026, 8, 18), end=date(2026, 9, 16)),
        arun.ArunOpenReceivedQuery(start=date(2024, 1, 1), end=date(2024, 1, 31)),
    )

    requests = tuple(arun._initial_search_request(form, query) for query in queries)

    assert all(request.method == RequestMethod.POST for request in requests)
    assert all(str(request.url).rstrip("/") == arun._SEARCH_URL for request in requests)
    received, decided, open_received = (
        {field.name: field.value for field in request.form} for request in requests
    )
    assert received == {
        "reference": "",
        "location": "",
        "OcellaPlanningSearch.postcode": "",
        "area": "",
        "applicant": "",
        "agent": "",
        "undecided": "",
        "type": "",
        "receivedFrom": "18-08-26",
        "receivedTo": "16-09-26",
        "decidedFrom": "",
        "decidedTo": "",
        "action": "Search",
    }
    assert decided["decidedFrom"] == "18-08-26"
    assert decided["decidedTo"] == "16-09-26"
    assert decided["receivedFrom"] == ""
    assert open_received["undecided"] == "Y"
    assert open_received["receivedFrom"] == "01-01-24"
    assert open_received["receivedTo"] == "31-01-24"


def test_arun_show_all_form_must_exactly_replay_the_active_query() -> None:
    query = arun.ArunReceivedQuery(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
    )
    fields = "".join(
        f'<input type="hidden" name="{name}" value="{value}">'
        for name, value in {
            "reference": "",
            "location": "",
            "OcellaPlanningSearch.postcode": "",
            "area": "",
            "applicant": "",
            "agent": "",
            "undecided": "",
            "type": "",
            "receivedFrom": "18-08-26",
            "receivedTo": "16-09-26",
            "decidedFrom": "",
            "decidedTo": "",
        }.items()
    )

    results = arun._parse_search_results(_partial_results(fields))
    request = arun._show_all_request(results.show_all_form, query)

    assert results.reported == 2
    assert [reference.reference for reference in results.references] == ["BR/1/26/PL"]
    assert request.method == RequestMethod.POST
    assert [(field.name, field.value) for field in request.form][:2] == [
        ("action", "Search"),
        ("showall", "showall"),
    ]

    wrong = fields.replace("18-08-26", "19-08-26")
    wrong_results = arun._parse_search_results(_partial_results(wrong))
    with pytest.raises(arun.ArunQueryReplayError):
        arun._show_all_request(wrong_results.show_all_form, query)


def test_arun_result_parser_fails_closed_on_the_portal_cap() -> None:
    with pytest.raises(arun.ArunResultCapError):
        arun._parse_search_results(
            b"Entered search criteria will retrieve more than 200 results"
        )

    empty = arun._parse_search_results(
        b"No applications found for entered search criteria"
    )
    assert empty.reported == 0
    assert empty.references == ()
