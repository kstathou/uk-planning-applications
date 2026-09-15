# Copyright (c) 2026 Kostas Stathoulopoulos

"""Haringey Salesforce register adapter."""

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

SOURCE = SourceId("haringey-salesforce-register")
BASE_URL = "https://londonboroughofharingey.my.site.com/pr/s"


class HaringeyCheckpointV1(FrozenModel):
    """Haringey Salesforce result cursor."""

    page_token: str


class HaringeyApplicationV1(FrozenModel):
    """Haringey-native application data returned by the public widget."""

    case_number: str
    proposal_text: str
    public_stage: str
    salesforce_record_id: str


class HaringeyParseError(ValueError):
    """A required Haringey widget field was absent."""

    def __init__(self, field: str) -> None:
        """Name the missing widget field."""
        super().__init__(f"missing Haringey field {field}")


class HaringeyAdapter:
    """Keep Haringey's JavaScript record vocabulary local."""

    manifest = AuthorityManifest(
        id=AuthorityId("haringey"),
        name="London Borough of Haringey",
        kind=AuthorityKind.LONDON_BOROUGH,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(f"{BASE_URL}/")),),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: HaringeyCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[HaringeyCheckpointV1]]:
        """Read sanitised results captured behind the Salesforce shell."""
        cursor = "first" if checkpoint is None else checkpoint.page_token
        url = (
            f"{BASE_URL}/search?from={window.start.isoformat()}"
            f"&to={window.end.isoformat()}&page={quote(cursor)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=SOURCE, reference=value)
            for value in re.findall(r'data-haringey-reference="([^"]+)"', html)
        )
        next_page = _required(
            html,
            r'data-haringey-page="([^"]+)"',
            "page token",
        )
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=HaringeyCheckpointV1(page_token=next_page),
            complete=next_page == "complete",
        )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[HaringeyApplicationV1]:
        """Read a sanitised rendered record without claiming live widget parity."""
        encoded = quote(reference.reference, safe="")
        url = f"{BASE_URL}/application/{encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        payload = HaringeyApplicationV1(
            case_number=reference.reference,
            proposal_text=unescape(
                _required(html, r'data-haringey-proposal="([^"]+)"', "proposal")
            ),
            public_stage=_required(
                html,
                r'data-haringey-stage="([^"]+)"',
                "stage",
            ),
            salesforce_record_id=_required(
                html,
                r'data-haringey-record-id="([^"]+)"',
                "record id",
            ),
        )
        unavailable = UnavailableSection(reason="JavaScript section not verified live")
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
        snapshot: NativeSnapshot[HaringeyApplicationV1],
    ) -> NormalisedObservation:
        """Map Haringey stage and proposal fields to the common record."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.proposal_text,
            status=snapshot.payload.public_stage.casefold().replace(" ", "-"),
            documents=(),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="haringey-v1",
        )


def _required(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise HaringeyParseError(field)
    return match.group(1).strip()
