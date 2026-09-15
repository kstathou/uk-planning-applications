# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001

"""Run the current pilot registry against sanitised fixtures."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from yimby import AuthorityId, Collector, DiscoveryWindow, pilot_registry
from yimby.authorities.arun.fixtures import fixture_session as arun_fixtures
from yimby.authorities.barnet.fixtures import fixture_session as barnet_fixtures
from yimby.authorities.birmingham.fixtures import fixture_session as birmingham_fixtures
from yimby.authorities.blackburn_with_darwen.fixtures import (
    fixture_session as blackburn_fixtures,
)
from yimby.authorities.camden.fixtures import fixture_session as camden_fixtures
from yimby.authorities.cheshire_east.fixtures import (
    fixture_session as cheshire_east_fixtures,
)
from yimby.authorities.cornwall.fixtures import fixture_session as cornwall_fixtures
from yimby.authorities.devon.fixtures import fixture_session as devon_fixtures
from yimby.authorities.dorset.fixtures import fixture_session as dorset_fixtures
from yimby.authorities.durham.fixtures import fixture_session as durham_fixtures
from yimby.authorities.haringey.fixtures import fixture_session as haringey_fixtures
from yimby.authorities.leeds.fixtures import fixture_session as leeds_fixtures
from yimby.authorities.opdc.fixtures import fixture_session as opdc_fixtures
from yimby.authorities.peak_district.fixtures import (
    fixture_session as peak_district_fixtures,
)
from yimby.authorities.west_suffolk.fixtures import (
    fixture_session as west_suffolk_fixtures,
)
from yimby.evidence import EvidenceStore
from yimby.store import SqliteStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from yimby.transport import FixtureSession

LOGGER = logging.getLogger(__name__)
EXPECTED_AUTHORITY_COUNT = 15

FIXTURES: dict[AuthorityId, Callable[[DiscoveryWindow], FixtureSession]] = {
    AuthorityId("barnet"): barnet_fixtures,
    AuthorityId("camden"): camden_fixtures,
    AuthorityId("haringey"): haringey_fixtures,
    AuthorityId("devon"): devon_fixtures,
    AuthorityId("peak-district"): peak_district_fixtures,
    AuthorityId("arun"): arun_fixtures,
    AuthorityId("opdc"): opdc_fixtures,
    AuthorityId("dorset"): dorset_fixtures,
    AuthorityId("cheshire-east"): cheshire_east_fixtures,
    AuthorityId("blackburn-with-darwen"): blackburn_fixtures,
    AuthorityId("birmingham"): birmingham_fixtures,
    AuthorityId("leeds"): leeds_fixtures,
    AuthorityId("cornwall"): cornwall_fixtures,
    AuthorityId("durham"): durham_fixtures,
    AuthorityId("west-suffolk"): west_suffolk_fixtures,
}


class AttachmentVerificationError(RuntimeError):
    """Fixture verification observed an attachment body request."""


class AuthorityCountVerificationError(RuntimeError):
    """Fixture verification did not complete the exact pilot registry."""


async def verify() -> None:
    """Collect every pilot fixture and enforce count and attachment policy."""
    window = DiscoveryWindow(
        start=date(2026, 8, 16),
        end=date(2026, 9, 15),
    )
    registry = pilot_registry()
    verified: list[AuthorityId] = []
    attachment_body_requests = 0
    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = SqliteStore(root / "yimby.sqlite3", EvidenceStore(root / "evidence"))
        collector = Collector(registry, store)
        for authority_id in registry.ids():
            report = await collector.collect(
                authority_id,
                window,
                FIXTURES[authority_id](window),
            )
            verified.append(authority_id)
            attachment_body_requests += report.attachment_body_requests
        store.close()
    if len(verified) != EXPECTED_AUTHORITY_COUNT or tuple(verified) != registry.ids():
        raise AuthorityCountVerificationError
    if attachment_body_requests != 0:
        raise AttachmentVerificationError
    LOGGER.info("verified %d pilot authority fixtures", len(verified))


if __name__ == "__main__":
    asyncio.run(verify())
