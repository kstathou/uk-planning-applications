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
from yimby.domain import (
    AuthorityId,
    AuthorityManifest,
    LiveReadiness,
    LiveStatus,
    LiveTransportKind,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from yimby.adapters import RunnableAuthority


def _status(
    readiness: LiveReadiness,
    reason: str,
    evidence: str,
    transport: LiveTransportKind | None = None,
) -> LiveStatus:
    return LiveStatus(
        readiness=readiness,
        reason=reason,
        evidence=(evidence,),
        transport=transport,
    )


PILOT_LIVE_STATUS: dict[AuthorityId, LiveStatus] = {
    AuthorityId("barnet"): _status(
        LiveReadiness.DISCOVERY_ONLY,
        "weekly live collection is bounded but older-open enumeration is unresolved",
        "captured weekly IDOX request and detail-tab contracts",
        LiveTransportKind.HTTP,
    ),
    AuthorityId("camden"): _status(
        LiveReadiness.DISCOVERY_ONLY,
        "exact-reference detail and document collection are implemented; "
        "bounded enumeration is unresolved",
        "captured Camden JSF, Northgate, and CMWebDrawer contracts",
        LiveTransportKind.HTTP,
    ),
    AuthorityId("haringey"): _status(
        LiveReadiness.BROWSER_ONLY,
        "the rolling seven-day browser journey is implemented; advanced and "
        "older-open enumeration remain unresolved",
        "captured Arcus quick-link, pagination, detail, comments, and "
        "file-tab selectors",
        LiveTransportKind.BROWSER,
    ),
    AuthorityId("devon"): _status(
        LiveReadiness.DISCOVERY_ONLY,
        "rolling 90-day discovery and detail collection are implemented; "
        "older-open and other windows are unresolved",
        "captured Devon disclaimer, rolling search, detail, and document contracts",
        LiveTransportKind.HTTP,
    ),
    AuthorityId("peak-district"): _status(
        LiveReadiness.DISCOVERY_ONLY,
        "rolling weekly discovery and visible summary are implemented; "
        "client-loaded sections and older-open are unresolved",
        "captured Peak District weekly DataTable and legacy summary contracts",
        LiveTransportKind.HTTP,
    ),
    AuthorityId("arun"): _status(
        LiveReadiness.DISCOVERY_ONLY,
        "bounded received-date discovery and visible detail are implemented; "
        "older-open and document actions are unresolved",
        "captured Arun Ocella received search, show-all, and detail contracts",
        LiveTransportKind.HTTP,
    ),
    AuthorityId("opdc"): _status(
        LiveReadiness.LIVE_READY,
        "official Agile API bootstrap and immediate idempotent rerun qualified",
        "docs/evidence/opdc-qualification-2026-09-16.json records "
        "55 complete applications",
        LiveTransportKind.HTTP,
    ),
    AuthorityId("dorset"): _status(
        LiveReadiness.BROWSER_ONLY,
        "the JavaScript map has no bounded implemented discovery path",
        "portal inventory records a client-rendered map boundary",
        LiveTransportKind.BROWSER,
    ),
    AuthorityId("cheshire-east"): _status(
        LiveReadiness.DISCOVERY_ONLY,
        "valid-date-from table discovery is implemented but count, pagination, "
        "window fidelity, and detail remain unresolved",
        "captured Cheshire East form controls, result table, and numeric View locator",
        LiveTransportKind.HTTP,
    ),
    AuthorityId("blackburn-with-darwen"): _status(
        LiveReadiness.BROWSER_ONLY,
        "bounded discovery and detail collection require visible Chromium, and "
        "same-day qualification is not yet complete",
        "captured current Citizen search, detail, document-metadata, result-cap, "
        "and human-verification contracts",
        LiveTransportKind.BROWSER,
    ),
    AuthorityId("birmingham"): _status(
        LiveReadiness.BLOCKED,
        "the investigated public portal returned HTTP 503",
        "portal inventory records an HTTP 503 boundary",
    ),
    AuthorityId("leeds"): _status(
        LiveReadiness.DISCOVERY_ONLY,
        "weekly discovery is implemented but the verified detail route fails remotely",
        "captured Leeds IDOX weekly pagination and remote-exception contracts",
        LiveTransportKind.HTTP,
    ),
    AuthorityId("cornwall"): _status(
        LiveReadiness.DISCOVERY_ONLY,
        "weekly and recorded detail collection are implemented; "
        "older-open is unresolved",
        "captured Cornwall IDOX weekly, summary, document, and zero-comment contracts",
        LiveTransportKind.HTTP,
    ),
    AuthorityId("durham"): _status(
        LiveReadiness.DISCOVERY_ONLY,
        "weekly and recorded detail collection are implemented; "
        "older-open is unresolved",
        "captured Durham IDOX weekly, summary, document, and "
        "comment-availability contracts",
        LiveTransportKind.HTTP,
    ),
    AuthorityId("west-suffolk"): _status(
        LiveReadiness.LIVE_READY,
        "live bootstrap and zero-request rerun passed on 16 September 2026",
        "docs/evidence/west-suffolk-qualification-2026-09-16.json",
        LiveTransportKind.HTTP,
    ),
}


class DuplicateAuthorityError(ValueError):
    """Two packages claim the same authority identifier."""

    def __init__(self) -> None:
        """Describe the conflicting ownership claim."""
        super().__init__("duplicate authority id")


class AuthorityRegistry:
    """Resolve each authority to one independently owned package."""

    def __init__(
        self,
        packages: Iterable[RunnableAuthority],
        live_statuses: dict[AuthorityId, LiveStatus] | None = None,
    ) -> None:
        """Index packages by their stable authority identifier."""
        package_list = tuple(packages)
        self._packages = {package.manifest.id: package for package in package_list}
        if len(self._packages) != len(package_list):
            raise DuplicateAuthorityError
        statuses = live_statuses or {}
        self._manifests = {
            authority_id: package.manifest.model_copy(
                update={
                    "live_status": statuses.get(
                        authority_id, package.manifest.live_status
                    )
                }
            )
            for authority_id, package in self._packages.items()
        }

    def get(self, authority_id: AuthorityId) -> RunnableAuthority:
        """Return the registered authority package."""
        return self._packages[authority_id]

    def ids(self) -> tuple[AuthorityId, ...]:
        """Return registered authority identifiers in stable order."""
        return tuple(self._packages)

    def manifest(self, authority_id: AuthorityId) -> AuthorityManifest:
        """Return the operational manifest for one authority."""
        return self._manifests[authority_id]

    def manifests(self) -> tuple[AuthorityManifest, ...]:
        """Return typed manifests in the same stable ownership order."""
        return tuple(self._manifests.values())


def barnet_registry() -> AuthorityRegistry:
    """Return the unit-one registry containing Barnet."""
    return AuthorityRegistry((BARNET_PACKAGE,), PILOT_LIVE_STATUS)


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
        ),
        PILOT_LIVE_STATUS,
    )
