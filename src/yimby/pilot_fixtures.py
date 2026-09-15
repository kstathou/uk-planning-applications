# Copyright (c) 2026 Kostas Stathoulopoulos

"""Explicit fixture transport catalog for local pilot operation."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
from yimby.domain import AuthorityId

if TYPE_CHECKING:
    from collections.abc import Callable

    from yimby.domain import DiscoveryWindow
    from yimby.transport import FixtureSession

FIXTURE_BUILDERS: dict[AuthorityId, Callable[[DiscoveryWindow], FixtureSession]] = {
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
