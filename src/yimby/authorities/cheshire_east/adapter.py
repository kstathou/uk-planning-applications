# Copyright (c) 2026 Kostas Stathoulopoulos

"""Cheshire East custom register adapter."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import HttpUrl

from yimby.domain import (
    AuthorityId,
    AuthorityKind,
    AuthorityManifest,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryWindow,
    FrozenModel,
    NativeSnapshot,
    NormalisedObservation,
    Provenance,
    SourceDefinition,
    SourceId,
    SourceReference,
    UnavailableSection,
)
from yimby.transport import PortalRequest, RequestIntent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession

SOURCE = SourceId("cheshire-east-custom-register")
BASE_URL = "https://pa.cheshireeast.gov.uk/planning"


class CheshireEastCheckpointV1(FrozenModel):
    """Cheshire East result-page cursor."""

    search_page: str


class CheshireEastApplicationV1(FrozenModel):
    """Cheshire East-native planning record."""

    public_reference: str
    alternative_reference: str
    development_proposal: str
    case_status: str
    parish_name: str


class CheshireEastParseError(ValueError):
    """A required Cheshire East field was absent."""

    def __init__(self, field: str) -> None:
        """Name the missing field."""
        super().__init__(f"missing Cheshire East field {field}")


class CheshireEastAdapter:
    """Keep Cheshire East search and reference rules local."""

    manifest = AuthorityManifest(
        id=AuthorityId("cheshire-east"),
        name="Cheshire East Council",
        kind=AuthorityKind.UNITARY,
        sources=(
            SourceDefinition(
                id=SOURCE,
                base_url=HttpUrl(f"{BASE_URL}/index.html?fa=search"),
            ),
        ),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: CheshireEastCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[CheshireEastCheckpointV1]]:
        """Read a Cheshire East custom search capture."""
        page = "1" if checkpoint is None else checkpoint.search_page
        url = (
            f"{BASE_URL}/search?from={window.start.isoformat()}"
            f"&to={window.end.isoformat()}&page={quote(page)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=SOURCE, reference=value)
            for value in re.findall(r'data-cheshire-reference="([^"]+)"', html)
        )
        next_page = _required(
            html,
            r'data-cheshire-page="([^"]+)"',
            "page",
        )
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=CheshireEastCheckpointV1(search_page=next_page),
            complete=next_page == "complete",
        )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[CheshireEastApplicationV1]:
        """Read a fixture record without inferring unobserved live sections."""
        encoded = quote(reference.reference, safe="")
        url = f"{BASE_URL}/application/{encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        payload = CheshireEastApplicationV1(
            public_reference=reference.reference,
            alternative_reference=_required(
                html,
                r'data-cheshire-alt="([^"]+)"',
                "alternative reference",
            ),
            development_proposal=unescape(
                _required(html, r'data-cheshire-proposal="([^"]+)"', "proposal")
            ),
            case_status=_required(
                html,
                r'data-cheshire-status="([^"]+)"',
                "status",
            ),
            parish_name=_required(
                html,
                r'data-cheshire-parish="([^"]+)"',
                "parish",
            ),
        )
        unavailable = UnavailableSection(reason="detail sections not verified live")
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=unavailable,
                comments=unavailable,
            ),
            evidence=(detail,),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[CheshireEastApplicationV1],
    ) -> NormalisedObservation:
        """Map Cheshire East fixture fields to the common record."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.development_proposal,
            status=snapshot.payload.case_status.casefold().replace(" ", "-"),
            documents=(),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="cheshire-east-v1",
        )


def _required(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise CheshireEastParseError(field)
    return match.group(1).strip()
