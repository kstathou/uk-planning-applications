# Copyright (c) 2026 Kostas Stathoulopoulos

"""Barnet discovery, extraction, and normalisation."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from html import unescape
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import HttpUrl

from yimby.domain import (
    AuthorityId,
    AuthorityKind,
    AuthorityManifest,
    CommentRecord,
    Completeness,
    CompleteSection,
    DiscoveryBatch,
    DiscoveryWindow,
    DocumentRecord,
    EmptySection,
    FailedSection,
    FrozenModel,
    NativeComment,
    NativeDocument,
    NativeSnapshot,
    NormalisedObservation,
    Provenance,
    SourceDefinition,
    SourceId,
    SourceReference,
)
from yimby.transport import (
    PortalRequest,
    RequestIntent,
    SourceUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession

CURRENT_SOURCE = SourceId("barnet-idox-current")
BASE_URL = "https://planningrecords.barnet.gov.uk"


class BarnetCheckpointV1(FrozenModel):
    """Barnet search cursor."""

    cursor: str


class BarnetApplicationV1(FrozenModel):
    """Barnet-native application payload."""

    proposal: str
    status: str
    documents: tuple[NativeDocument, ...]
    comments: tuple[NativeComment, ...]


class BarnetAdapter:
    """Collect Barnet records while keeping portal rules local."""

    manifest = AuthorityManifest(
        id=AuthorityId("barnet"),
        name="London Borough of Barnet",
        kind=AuthorityKind.LONDON_BOROUGH,
        sources=(
            SourceDefinition(
                id=SourceId("barnet-idox-legacy"),
                base_url=HttpUrl("https://legacy-planningrecords.barnet.gov.uk"),
                valid_to=date(2020, 12, 31),
            ),
            SourceDefinition(
                id=CURRENT_SOURCE,
                base_url=HttpUrl(BASE_URL),
                valid_from=date(2021, 1, 1),
            ),
        ),
    )

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: BarnetCheckpointV1 | None,
    ) -> AsyncIterator[DiscoveryBatch[BarnetCheckpointV1]]:
        """Discover Barnet references from the sanitised weekly search shape."""
        cursor = "start" if checkpoint is None else checkpoint.cursor
        url = (
            f"{BASE_URL}/search?start={window.start.isoformat()}"
            f"&end={window.end.isoformat()}&cursor={quote(cursor)}"
        )
        capture = await session.fetch(
            PortalRequest(url=HttpUrl(url), intent=RequestIntent.SEARCH)
        )
        html = capture.body.decode()
        references = tuple(
            SourceReference(source_id=CURRENT_SOURCE, reference=value)
            for value in re.findall(r'data-reference="([^"]+)"', html)
        )
        next_cursor = _required(html, r'data-next-cursor="([^"]+)"')
        yield DiscoveryBatch(
            references=references,
            next_checkpoint=BarnetCheckpointV1(cursor=next_cursor),
            complete=next_cursor == "complete",
        )

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[BarnetApplicationV1]:
        """Fetch detail and exposed comment text without following documents."""
        detail_url = f"{BASE_URL}/application/{quote(reference.reference)}"
        detail = await session.fetch(
            PortalRequest(url=HttpUrl(detail_url), intent=RequestIntent.DETAIL)
        )
        html = detail.body.decode()
        documents = tuple(
            NativeDocument(title=unescape(title), url=HttpUrl(url))
            for title, url in re.findall(
                r'data-document-title="([^"]+)" data-document-url="([^"]+)"',
                html,
            )
        )
        comments_url = _required(html, r'data-comments-url="([^"]+)"')
        evidence = [detail]
        comments_state: CompleteSection | EmptySection | FailedSection
        try:
            comments_capture = await session.fetch(
                PortalRequest(
                    url=HttpUrl(comments_url),
                    intent=RequestIntent.COMMENTS,
                )
            )
        except SourceUnavailableError:
            comments: tuple[NativeComment, ...] = ()
            comments_state = FailedSection(code="source-unavailable")
        else:
            evidence.append(comments_capture)
            comments = tuple(
                NativeComment(comment_id=comment_id, text=unescape(text.strip()))
                for comment_id, text in re.findall(
                    r'data-comment-id="([^"]+)">([^<]+)</li>',
                    comments_capture.body.decode(),
                )
            )
            comments_state = _collection_state(len(comments))
        payload = BarnetApplicationV1(
            proposal=unescape(_required(html, r'id="proposal">([^<]+)</')),
            status=unescape(_required(html, r'id="status">([^<]+)</')),
            documents=documents,
            comments=comments,
        )
        return NativeSnapshot(
            reference=reference,
            observed_at=datetime.now(UTC),
            payload=payload,
            completeness=Completeness(
                application=CompleteSection(item_count=1),
                documents=_collection_state(len(documents)),
                comments=comments_state,
            ),
            evidence=tuple(evidence),
        )

    def normalise(
        self,
        snapshot: NativeSnapshot[BarnetApplicationV1],
    ) -> NormalisedObservation:
        """Map Barnet-native values to the common first-slice model."""
        evidence = snapshot.evidence[0].digest
        return NormalisedObservation(
            authority_id=self.manifest.id,
            reference=snapshot.reference,
            proposal=snapshot.payload.proposal,
            status=snapshot.payload.status.casefold().replace(" ", "-"),
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
            normaliser_version="barnet-v1",
        )


def _required(value: str, pattern: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise BarnetParseError(pattern)
    return match.group(1).strip()


def _collection_state(count: int) -> CompleteSection | EmptySection:
    if count == 0:
        return EmptySection()
    return CompleteSection(item_count=count)


class BarnetParseError(ValueError):
    """A required field was absent from a Barnet response."""

    def __init__(self, pattern: str) -> None:
        """Name the field pattern that failed."""
        super().__init__(f"required Barnet field did not match {pattern}")
