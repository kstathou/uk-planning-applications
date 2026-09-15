# Copyright (c) 2026 Kostas Stathoulopoulos

"""Birmingham Planning Explorer adapter."""

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

SOURCE = SourceId("birmingham-northgate-explorer")
BASE_URL = "https://eplanning.birmingham.gov.uk/Northgate/PlanningExplorer"


class BirminghamCheckpointV1(FrozenModel):
    """Birmingham Planning Explorer page cursor."""

    result_page: str


class BirminghamApplicationV1(FrozenModel):
    """Birmingham-native Planning Explorer record."""

    explorer_key: str
    application_reference: str
    proposal_text: str
    current_status: str
    ward_name: str


class BirminghamParseError(ValueError):
    """A required Birmingham field was absent."""

    def __init__(self, field: str) -> None:
        """Name the missing field."""
        super().__init__(f"missing Birmingham field {field}")


class BirminghamAdapter:
    """Keep Birmingham's Northgate record shape independent."""

    manifest = AuthorityManifest(
        id=AuthorityId("birmingham"),
        name="Birmingham City Council",
        kind=AuthorityKind.METROPOLITAN,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(f"{BASE_URL}/")),),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: BirminghamCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[BirminghamCheckpointV1]]:
        """Read a captured result rather than treating HTTP 503 as empty."""
        page = "1" if checkpoint is None else checkpoint.result_page
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
            for value in re.findall(r'data-birmingham-reference="([^"]+)"', html)
        )
        next_page = _required(
            html,
            r'data-birmingham-page="([^"]+)"',
            "page",
        )
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=BirminghamCheckpointV1(result_page=next_page),
            complete=next_page == "complete",
        )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[BirminghamApplicationV1]:
        """Read the sanitised Planning Explorer detail capture."""
        encoded = quote(reference.reference, safe="")
        url = f"{BASE_URL}/application/{encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        payload = BirminghamApplicationV1(
            explorer_key=_required(
                html,
                r'data-birmingham-key="([^"]+)"',
                "explorer key",
            ),
            application_reference=reference.reference,
            proposal_text=unescape(
                _required(html, r'data-birmingham-proposal="([^"]+)"', "proposal")
            ),
            current_status=_required(
                html,
                r'data-birmingham-status="([^"]+)"',
                "status",
            ),
            ward_name=_required(
                html,
                r'data-birmingham-ward="([^"]+)"',
                "ward",
            ),
        )
        unavailable = UnavailableSection(reason="live portal returned HTTP 503")
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
        snapshot: NativeSnapshot[BirminghamApplicationV1],
    ) -> NormalisedObservation:
        """Map Birmingham-native fields to the common record."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.proposal_text,
            status=snapshot.payload.current_status.casefold().replace(" ", "-"),
            documents=(),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="birmingham-v1",
        )


def _required(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise BirminghamParseError(field)
    return match.group(1).strip()
