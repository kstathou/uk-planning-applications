# Copyright (c) 2026 Kostas Stathoulopoulos

"""Public collection operation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from yimby.domain import (
    AuthorityId,
    CollectionReport,
    DiscoveryWindow,
)

if TYPE_CHECKING:
    from yimby.registry import AuthorityRegistry
    from yimby.store import SqliteStore
    from yimby.transport import PortalSession


class Collector:
    """Run one authority while hiding adapter and persistence coordination."""

    def __init__(self, registry: AuthorityRegistry, store: SqliteStore) -> None:
        """Bind an authority registry to its single writer."""
        self._registry = registry
        self._store = store

    async def collect(
        self,
        authority_id: AuthorityId,
        window: DiscoveryWindow,
        session: PortalSession,
    ) -> CollectionReport:
        """Discover, queue, fetch, normalise, and commit one authority."""
        package = self._registry.get(authority_id)
        run_id = self._store.begin_run(authority_id)
        checkpoint = self._store.discovery_state(authority_id).checkpoint
        application_ids = []
        attachment_urls: set[str] = set()
        async for batch in package.discover(session, window, checkpoint):
            self._store.commit_discovery(run_id, authority_id, batch)
            for reference in batch.references:
                collected = await package.collect(session, reference)
                attachment_urls.update(
                    str(document.url) for document in collected.normalised.documents
                )
                application_ids.append(
                    self._store.commit_observation(run_id, collected)
                )
        retrieved_attachment_urls = attachment_urls.intersection(session.requested_urls)
        return CollectionReport(
            applications=tuple(application_ids),
            requested_urls=session.requested_urls,
            attachment_body_requests=(
                session.attachment_body_requests + len(retrieved_attachment_urls)
            ),
        )
