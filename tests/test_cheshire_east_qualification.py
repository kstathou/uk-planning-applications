# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, PLR2004, SLF001

"""Cheshire East's official blocker and reliable source contracts."""

from __future__ import annotations

from datetime import date

import yimby.authorities.cheshire_east.adapter as cheshire
from yimby.domain import DiscoveryWindow


def _search_form() -> bytes:
    return b"""
    <form id="form" name="form" method="post" action="/planning/index.html">
      <input type="hidden" name="fa" value="search">
      <input type="hidden" name="submitted" value="">
      <input name="application_reference_number" value="">
      <select name="application_type_id">
        <option value="" selected>Any</option><option value="4">Full</option>
      </select>
      <input name="valid_date_from" value="">
      <input name="valid_date_to" value="">
      <input type="checkbox" name="ignored_checkbox" value="1">
      <input type="checkbox" name="included_checkbox" value="yes" checked>
      <input type="text" name="disabled_value" value="no" disabled>
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


def test_cheshire_replays_exact_successful_search_controls() -> None:
    form = cheshire._parse_search_form(_search_form())
    request = cheshire._valid_date_request(
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
        ("valid_date_from", "18-08-2026"),
        ("valid_date_to", "16-09-2026"),
        ("included_checkbox", "yes"),
    )


def test_cheshire_weekly_boundary_records_an_unproved_fifty_row_cap() -> None:
    form = cheshire._parse_weekly_form(_weekly_form())
    request = cheshire._weekly_received_request(form, date(2024, 1, 1))
    boundary = cheshire._parse_weekly_boundary(_weekly_results())

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
    detail = cheshire._parse_detail_contract(
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
