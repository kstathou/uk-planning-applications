# Copyright (c) 2026 Kostas Stathoulopoulos

"""Fixture contracts shared by all pilot authority packages."""

from __future__ import annotations

import asyncio
import gzip
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

import pytest

from yimby import AuthorityId, Collector, DiscoveryWindow, pilot_registry
from yimby.authorities.arun.adapter import ArunParseError
from yimby.authorities.arun.fixtures import fixture_session as arun_fixtures
from yimby.authorities.barnet.adapter import BarnetParseError
from yimby.authorities.barnet.fixtures import fixture_session as barnet_fixtures
from yimby.authorities.birmingham.adapter import BirminghamParseError
from yimby.authorities.birmingham.fixtures import fixture_session as birmingham_fixtures
from yimby.authorities.blackburn_with_darwen.adapter import (
    BlackburnWithDarwenParseError,
)
from yimby.authorities.blackburn_with_darwen.fixtures import (
    fixture_session as blackburn_fixtures,
)
from yimby.authorities.camden.adapter import CamdenParseError
from yimby.authorities.camden.fixtures import fixture_session as camden_fixtures
from yimby.authorities.cheshire_east.adapter import CheshireEastParseError
from yimby.authorities.cheshire_east.fixtures import (
    fixture_session as cheshire_east_fixtures,
)
from yimby.authorities.cornwall.adapter import CornwallParseError
from yimby.authorities.cornwall.fixtures import fixture_session as cornwall_fixtures
from yimby.authorities.devon.adapter import DevonParseError
from yimby.authorities.devon.fixtures import fixture_session as devon_fixtures
from yimby.authorities.dorset.adapter import DorsetParseError
from yimby.authorities.dorset.fixtures import fixture_session as dorset_fixtures
from yimby.authorities.durham.adapter import DurhamParseError
from yimby.authorities.durham.fixtures import fixture_session as durham_fixtures
from yimby.authorities.haringey.adapter import HaringeyParseError
from yimby.authorities.haringey.fixtures import fixture_session as haringey_fixtures
from yimby.authorities.leeds.adapter import LeedsParseError
from yimby.authorities.leeds.fixtures import fixture_session as leeds_fixtures
from yimby.authorities.opdc.adapter import OpdcParseError
from yimby.authorities.opdc.fixtures import fixture_session as opdc_fixtures
from yimby.authorities.peak_district.adapter import PeakDistrictParseError
from yimby.authorities.peak_district.fixtures import (
    fixture_session as peak_district_fixtures,
)
from yimby.authorities.west_suffolk.adapter import WestSuffolkParseError
from yimby.authorities.west_suffolk.fixtures import (
    fixture_session as west_suffolk_fixtures,
)
from yimby.evidence import EvidenceStore
from yimby.store import SqliteStore
from yimby.transport import FixtureResponse, FixtureSession

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

WINDOW = DiscoveryWindow(
    start=date(2026, 8, 16),
    end=date(2026, 9, 15),
)


@dataclass(frozen=True, slots=True)
class _PilotCase:
    authority_id: AuthorityId
    fixtures: Callable[[DiscoveryWindow], FixtureSession]
    parse_error: type[ValueError]
    proposal: str
    document_state: str
    comment_state: str
    document_versions: int
    comment_versions: int


CASES = (
    _PilotCase(
        AuthorityId("barnet"),
        barnet_fixtures,
        BarnetParseError,
        "Build two homes & plant four trees",
        "complete",
        "complete",
        1,
        1,
    ),
    _PilotCase(
        AuthorityId("camden"),
        camden_fixtures,
        CamdenParseError,
        "Repair listed townhouse",
        "complete",
        "unavailable",
        1,
        0,
    ),
    _PilotCase(
        AuthorityId("haringey"),
        haringey_fixtures,
        HaringeyParseError,
        "Extend the community hall",
        "unavailable",
        "unavailable",
        0,
        0,
    ),
    _PilotCase(
        AuthorityId("devon"),
        devon_fixtures,
        DevonParseError,
        "Upgrade recycling centre",
        "complete",
        "excluded",
        1,
        0,
    ),
    _PilotCase(
        AuthorityId("peak-district"),
        peak_district_fixtures,
        PeakDistrictParseError,
        "Discharge landscape condition",
        "unavailable",
        "unavailable",
        0,
        0,
    ),
    _PilotCase(
        AuthorityId("arun"),
        arun_fixtures,
        ArunParseError,
        "Build a detached home",
        "complete",
        "unavailable",
        1,
        0,
    ),
    _PilotCase(
        AuthorityId("opdc"),
        opdc_fixtures,
        OpdcParseError,
        "Mixed use development at Old Oak",
        "unavailable",
        "unavailable",
        0,
        0,
    ),
    _PilotCase(
        AuthorityId("dorset"),
        dorset_fixtures,
        DorsetParseError,
        "Convert barn to two homes",
        "unavailable",
        "unavailable",
        0,
        0,
    ),
    _PilotCase(
        AuthorityId("cheshire-east"),
        cheshire_east_fixtures,
        CheshireEastParseError,
        "Extend rural workshop",
        "unavailable",
        "unavailable",
        0,
        0,
    ),
    _PilotCase(
        AuthorityId("blackburn-with-darwen"),
        blackburn_fixtures,
        BlackburnWithDarwenParseError,
        "Refurbish former mill",
        "unavailable",
        "unavailable",
        0,
        0,
    ),
    _PilotCase(
        AuthorityId("birmingham"),
        birmingham_fixtures,
        BirminghamParseError,
        "Build affordable homes",
        "unavailable",
        "unavailable",
        0,
        0,
    ),
    _PilotCase(
        AuthorityId("leeds"),
        leeds_fixtures,
        LeedsParseError,
        "Extend city centre offices",
        "complete",
        "unavailable",
        1,
        0,
    ),
    _PilotCase(
        AuthorityId("cornwall"),
        cornwall_fixtures,
        CornwallParseError,
        "Install coastal solar array",
        "complete",
        "unavailable",
        1,
        0,
    ),
    _PilotCase(
        AuthorityId("durham"),
        durham_fixtures,
        DurhamParseError,
        "Expand county school",
        "complete",
        "unavailable",
        1,
        0,
    ),
    _PilotCase(
        AuthorityId("west-suffolk"),
        west_suffolk_fixtures,
        WestSuffolkParseError,
        "Prune protected garden trees",
        "complete",
        "unavailable",
        1,
        0,
    ),
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
def test_authority_fixture_contract(case: _PilotCase, tmp_path: Path) -> None:
    """Every package survives an unchanged public-API rerun."""
    root = tmp_path / str(case.authority_id)
    store = SqliteStore(root / "yimby.sqlite3", EvidenceStore(root / "evidence"))
    collector = Collector(pilot_registry(), store)

    first = asyncio.run(
        collector.collect(case.authority_id, WINDOW, case.fixtures(WINDOW))
    )
    second = asyncio.run(
        collector.collect(case.authority_id, WINDOW, case.fixtures(WINDOW))
    )

    assert len(first.applications) == 1
    assert second.applications == first.applications
    assert first.attachment_body_requests == 0
    assert second.attachment_body_requests == 0
    application_id = first.applications[0]
    stored = store.get_application(application_id)
    assert stored.authority_id == case.authority_id
    assert stored.proposal == case.proposal
    assert stored.completeness.application.kind == "complete"
    assert stored.completeness.documents.kind == case.document_state
    assert stored.completeness.comments.kind == case.comment_state
    assert store.semantic_version_count(application_id, "application") == 1
    assert (
        store.semantic_version_count(application_id, "documents")
        == case.document_versions
    )
    assert (
        store.semantic_version_count(application_id, "comments")
        == case.comment_versions
    )
    evidence_paths = tuple((root / "evidence").rglob("*.gz"))
    assert evidence_paths
    assert all(gzip.decompress(path.read_bytes()) for path in evidence_paths)
    store.close()


@pytest.mark.parametrize("case", CASES, ids=lambda case: str(case.authority_id))
def test_authority_rejects_malformed_search(
    case: _PilotCase,
    tmp_path: Path,
) -> None:
    """Missing authority-native cursor data fails before checkpoint commit."""
    valid_session = case.fixtures(WINDOW)
    malformed = FixtureSession(
        {
            valid_session.available_urls[0]: FixtureResponse(
                body=b"<html><body>malformed search</body></html>"
            )
        }
    )
    root = tmp_path / str(case.authority_id)
    store = SqliteStore(root / "yimby.sqlite3", EvidenceStore(root / "evidence"))
    collector = Collector(pilot_registry(), store)

    with pytest.raises(case.parse_error):
        asyncio.run(collector.collect(case.authority_id, WINDOW, malformed))

    assert store.discovery_state(case.authority_id).checkpoint is None
    store.close()


def test_pilot_registry_ownership() -> None:
    """Every pilot authority has one stable package owner."""
    assert pilot_registry().ids() == (
        AuthorityId("barnet"),
        AuthorityId("camden"),
        AuthorityId("haringey"),
        AuthorityId("devon"),
        AuthorityId("peak-district"),
        AuthorityId("arun"),
        AuthorityId("opdc"),
        AuthorityId("dorset"),
        AuthorityId("cheshire-east"),
        AuthorityId("blackburn-with-darwen"),
        AuthorityId("birmingham"),
        AuthorityId("leeds"),
        AuthorityId("cornwall"),
        AuthorityId("durham"),
        AuthorityId("west-suffolk"),
    )
