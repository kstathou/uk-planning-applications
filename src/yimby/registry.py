# Copyright (c) 2026 Kostas Stathoulopoulos

"""Authority package registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from yimby.authorities.arun import ARUN_PACKAGE
from yimby.authorities.barnet import BARNET_PACKAGE
from yimby.authorities.birmingham import BIRMINGHAM_PACKAGE
from yimby.authorities.blackburn_with_darwen import BLACKBURN_WITH_DARWEN_PACKAGE
from yimby.authorities.camden import CAMDEN_PACKAGE
from yimby.authorities.cheshire_east import CHESHIRE_EAST_PACKAGE
from yimby.authorities.cornwall import CORNWALL_PACKAGE
from yimby.authorities.devon import DEVON_PACKAGE
from yimby.authorities.dorset import DORSET_PACKAGE
from yimby.authorities.durham import DURHAM_PACKAGE
from yimby.authorities.haringey import HARINGEY_PACKAGE
from yimby.authorities.leeds import LEEDS_PACKAGE
from yimby.authorities.opdc import OPDC_PACKAGE
from yimby.authorities.peak_district import PEAK_DISTRICT_PACKAGE
from yimby.authorities.west_suffolk import WEST_SUFFOLK_PACKAGE

if TYPE_CHECKING:
    from collections.abc import Iterable

    from yimby.adapters import RunnableAuthority
    from yimby.domain import AuthorityId


class DuplicateAuthorityError(ValueError):
    """Two packages claim the same authority identifier."""

    def __init__(self) -> None:
        """Describe the conflicting ownership claim."""
        super().__init__("duplicate authority id")


class AuthorityRegistry:
    """Resolve each authority to one independently owned package."""

    def __init__(self, packages: Iterable[RunnableAuthority]) -> None:
        """Index packages by their stable authority identifier."""
        package_list = tuple(packages)
        self._packages = {package.manifest.id: package for package in package_list}
        if len(self._packages) != len(package_list):
            raise DuplicateAuthorityError

    def get(self, authority_id: AuthorityId) -> RunnableAuthority:
        """Return the registered authority package."""
        return self._packages[authority_id]

    def ids(self) -> tuple[AuthorityId, ...]:
        """Return registered authority identifiers in stable order."""
        return tuple(self._packages)


def barnet_registry() -> AuthorityRegistry:
    """Return the unit-one registry containing Barnet."""
    return AuthorityRegistry((BARNET_PACKAGE,))


def pilot_registry() -> AuthorityRegistry:
    """Return all fifteen pilot authorities in delivery order."""
    return AuthorityRegistry(
        (
            BARNET_PACKAGE,
            CAMDEN_PACKAGE,
            HARINGEY_PACKAGE,
            DEVON_PACKAGE,
            PEAK_DISTRICT_PACKAGE,
            ARUN_PACKAGE,
            OPDC_PACKAGE,
            DORSET_PACKAGE,
            CHESHIRE_EAST_PACKAGE,
            BLACKBURN_WITH_DARWEN_PACKAGE,
            BIRMINGHAM_PACKAGE,
            LEEDS_PACKAGE,
            CORNWALL_PACKAGE,
            DURHAM_PACKAGE,
            WEST_SUFFOLK_PACKAGE,
        )
    )
