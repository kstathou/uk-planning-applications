# Copyright (c) 2026 Kostas Stathoulopoulos

"""Leeds IDOX Public Access adapter."""

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
    DocumentRecord,
    FrozenModel,
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
from yimby.transport import PortalRequest, RequestIntent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession

SOURCE = SourceId("leeds-idox-public-access")
BASE_URL = "https://publicaccess.leeds.gov.uk/online-applications"


class LeedsCheckpointV1(FrozenModel):
    """Leeds IDOX result-page cursor."""

    result_page: str


class LeedsApplicationV1(FrozenModel):
    """Leeds-native IDOX application."""

    idox_key: str
    application_reference: str
    proposal_text: str
    case_status: str
    application_type: str
    documents: tuple[NativeDocument, ...]


class LeedsParseError(ValueError):
    """A required Leeds field was absent."""

    def __init__(self, field: str) -> None:
        """Name the missing field."""
        super().__init__(f"missing Leeds field {field}")


class LeedsAdapter:
    """Own Leeds IDOX fields and publication policy."""

    manifest = AuthorityManifest(
        id=AuthorityId("leeds"),
        name="Leeds City Council",
        kind=AuthorityKind.METROPOLITAN,
        sources=(SourceDefinition(id=SOURCE, base_url=HttpUrl(f"{BASE_URL}/")),),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: LeedsCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[LeedsCheckpointV1]]:
        """Discover Leeds application references from a captured result page."""
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
            for value in re.findall(r'data-leeds-reference="([^"]+)"', html)
        )
        next_page = _required(html, r'data-leeds-page="([^"]+)"', "page")
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=LeedsCheckpointV1(result_page=next_page),
            complete=next_page == "complete",
        )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[LeedsApplicationV1]:
        """Read Leeds detail and document metadata without attachment bodies."""
        encoded = quote(reference.reference, safe="")
        url = f"{BASE_URL}/application/{encoded}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        documents = tuple(
            NativeDocument(title=unescape(title), url=HttpUrl(document_url))
            for title, document_url in re.findall(
                r'data-leeds-document="([^"]+)" href="([^"]+)"', html
            )
        )
        payload = LeedsApplicationV1(
            idox_key=_required(html, r'data-leeds-key="([^"]+)"', "IDOX key"),
            application_reference=reference.reference,
            proposal_text=unescape(
                _required(html, r'data-leeds-proposal="([^"]+)"', "proposal")
            ),
            case_status=_required(
                html,
                r'data-leeds-status="([^"]+)"',
                "status",
            ),
            application_type=_required(
                html,
                r'data-leeds-type="([^"]+)"',
                "application type",
            ),
            documents=documents,
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=collection_state(len(documents)),
                comments=UnavailableSection(
                    reason="public comment text is not published"
                ),
            ),
            evidence=(detail,),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[LeedsApplicationV1],
    ) -> NormalisedObservation:
        """Map Leeds-native fields to the common record."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.proposal_text,
            status=snapshot.payload.case_status.casefold().replace(" ", "-"),
            documents=tuple(
                DocumentRecord(title=item.title, url=item.url)
                for item in snapshot.payload.documents
            ),
            comments=(),
            completeness=snapshot.completeness,
            provenance=(
                Provenance(field="proposal", evidence=evidence),
                Provenance(field="status", evidence=evidence),
            ),
            normaliser_version="leeds-v1",
        )


def _required(value: str, pattern: str, field: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise LeedsParseError(field)
    return match.group(1).strip()
