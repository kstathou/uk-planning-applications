# Copyright (c) 2026 Kostas Stathoulopoulos

"""Camden discovery, extraction, and normalisation."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from html import unescape
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import HttpUrl

from yimby.domain import (
    ApplicationMetadata,
    AuthorityId,
    AuthorityKind,
    AuthorityManifest,
    CommentRecord,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryWindow,
    DocumentRecord,
    FrozenModel,
    NativeComment,
    NativeDocument,
    NativeSnapshot,
    NormalisedObservation,
    Provenance,
    SourceDefinition,
    SourceId,
    SourceReference,
    UnavailableSection,
    collection_state,
)
from yimby.geo import bng_to_wgs84
from yimby.transport import PortalRequest, RequestIntent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession

SEARCH_SOURCE = SourceId("camden-jsf-search")
SEARCH_BASE = "https://accountforms.camden.gov.uk/planning-search"
DETAIL_BASE = "https://planningrecords.camden.gov.uk/NECSWS/PlanningExplorer"


class CamdenCheckpointV1(FrozenModel):
    """Camden JSF search cursor."""

    view_state_page: str


class CamdenApplicationV1(FrozenModel):
    """Camden-native record spanning search, detail, and document services."""

    public_reference: str
    proposal: str
    current_status: str
    grid_easting: int
    grid_northing: int
    documents: tuple[NativeDocument, ...]
    comments: tuple[NativeComment, ...]


class CamdenParseError(ValueError):
    """A required Camden field was absent."""

    def __init__(self, field: str) -> None:
        """Name the missing field."""
        super().__init__(f"missing Camden field {field}")


class CamdenAdapter:
    """Keep Camden's three-service record assembly authority-local."""

    manifest = AuthorityManifest(
        id=AuthorityId("camden"),
        name="London Borough of Camden",
        kind=AuthorityKind.LONDON_BOROUGH,
        sources=(
            SourceDefinition(
                id=SEARCH_SOURCE,
                base_url=HttpUrl(f"{SEARCH_BASE}/"),
                valid_from=date(2010, 1, 1),
            ),
            SourceDefinition(
                id=SourceId("camden-northgate-records"),
                base_url=HttpUrl(f"{DETAIL_BASE}/"),
            ),
            SourceDefinition(
                id=SourceId("camden-cmwebdrawer-documents"),
                base_url=HttpUrl("https://camdocs.camden.gov.uk/CMWebDrawer/PlanRec"),
            ),
        ),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: CamdenCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[CamdenCheckpointV1]]:
        """Read references from a sanitised JSF result capture."""
        cursor = "initial" if checkpoint is None else checkpoint.view_state_page
        url = (
            f"{SEARCH_BASE}/search?from={window.start.isoformat()}"
            f"&to={window.end.isoformat()}&view={quote(cursor)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=SEARCH_SOURCE, reference=value)
            for value in re.findall(r'data-camden-reference="([^"]+)"', html)
        )
        next_page = _required(html, r'data-camden-next="([^"]+)"', "next page")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=CamdenCheckpointV1(view_state_page=next_page),
            complete=next_page == "complete",
        )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[CamdenApplicationV1]:
        """Fetch a Northgate detail capture with document metadata."""
        encoded = quote(reference.reference, safe="")
        url = f"{DETAIL_BASE}/application?reference={encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        documents = tuple(
            NativeDocument(title=unescape(title), url=HttpUrl(document_url))
            for title, document_url in re.findall(
                r'data-camden-document="([^"]+)" href="([^"]+)"', html
            )
        )
        payload = CamdenApplicationV1(
            public_reference=reference.reference,
            proposal=unescape(
                _required(html, r'data-camden-proposal="([^"]+)"', "proposal")
            ),
            current_status=_required(
                html,
                r'data-camden-status="([^"]+)"',
                "status",
            ),
            grid_easting=int(
                _required(html, r'data-camden-easting="([^"]+)"', "easting")
            ),
            grid_northing=int(
                _required(html, r'data-camden-northing="([^"]+)"', "northing")
            ),
            documents=documents,
            comments=(),
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=collection_state(len(documents)),
                comments=UnavailableSection(reason="comment enumeration not verified"),
            ),
            evidence=(detail,),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[CamdenApplicationV1],
    ) -> NormalisedObservation:
        """Map Camden-native names while preserving native coordinates."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.proposal,
            status=snapshot.payload.current_status.casefold().replace(" ", "-"),
            documents=tuple(
                DocumentRecord(title=item.title, url=item.url)
                for item in snapshot.payload.documents
            ),
            comments=tuple(
                CommentRecord(comment_id=item.comment_id, text=item.text)
                for item in snapshot.payload.comments
            ),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="camden-v1",
            metadata=ApplicationMetadata(
                location=bng_to_wgs84(
                    snapshot.payload.grid_easting,
                    snapshot.payload.grid_northing,
                )
            ),
        )


def _required(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise CamdenParseError(field)
    return match.group(1).strip()
