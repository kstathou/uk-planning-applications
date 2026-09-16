# Copyright (c) 2026 Kostas Stathoulopoulos

"""Public collection operation."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from yimby.domain import (
    AuthorityId,
    CollectionReport,
    DiscoveryWindow,
    RunMetrics,
    RunOutcome,
    RunStatus,
    SourceReference,
)

if TYPE_CHECKING:
    from yimby.registry import AuthorityRegistry
    from yimby.store import SqliteStore
    from yimby.transport import PortalSession


@dataclass(slots=True)
class _RunContext:
    run_id: str
    authority_id: AuthorityId
    session: PortalSession
    started: float
    storage_before: int
    active_reference: SourceReference | None = None
    attachment_urls: list[str] = field(default_factory=list)


class Collector:
    """Run one authority while hiding adapter and persistence coordination."""

    def __init__(self, registry: AuthorityRegistry, store: SqliteStore) -> None:
        """Bind an authority registry to its single writer."""
        self._registry = registry
        self._store = store
        self._store.register_authorities(registry.manifests())

    async def collect(
        self,
        authority_id: AuthorityId,
        window: DiscoveryWindow,
        session: PortalSession,
    ) -> CollectionReport:
        """Discover, queue, fetch, normalise, and commit one authority."""
        package = self._registry.get(authority_id)
        run_id = self._store.begin_run(authority_id)
        context = _RunContext(
            run_id=run_id,
            authority_id=authority_id,
            session=session,
            started=monotonic(),
            storage_before=self._store.storage_bytes(),
        )
        checkpoint = self._store.discovery_state(authority_id).checkpoint
        application_ids = []
        processed: set[tuple[str, str]] = set()

        async def collect_reference(reference: SourceReference) -> None:
            key = (str(reference.source_id), reference.reference)
            if key in processed:
                return
            context.active_reference = reference
            collected = await package.collect(session, reference)
            context.attachment_urls.extend(
                str(document.url) for document in collected.normalised.documents
            )
            application_ids.append(self._store.commit_observation(run_id, collected))
            self._store.mark_retry_succeeded(authority_id, reference)
            processed.add(key)
            context.active_reference = None

        try:
            for reference in self._store.collection_work(
                authority_id,
                datetime.now(UTC),
            ):
                await collect_reference(reference)
            async for batch in package.discover(session, window, checkpoint):
                self._store.commit_discovery(run_id, authority_id, batch)
                for reference in batch.references:
                    await collect_reference(reference)
        except asyncio.CancelledError as error:
            self._finish_failed_run(
                context,
                RunStatus.INTERRUPTED,
                error,
            )
            raise
        except Exception as error:
            self._finish_failed_run(
                context,
                RunStatus.FAILED,
                error,
            )
            raise
        attachment_body_requests = self._attachment_body_requests(context)
        self._store.finish_run(
            run_id,
            authority_id,
            RunOutcome(
                status=RunStatus.SUCCEEDED,
                metrics=self._metrics(context),
                transport_mode=session.mode,
            ),
        )
        return CollectionReport(
            applications=tuple(application_ids),
            requested_urls=session.requested_urls,
            attachment_body_requests=attachment_body_requests,
        )

    def _finish_failed_run(
        self,
        context: _RunContext,
        status: RunStatus,
        error: BaseException,
    ) -> None:
        error_name = type(error).__name__
        if context.active_reference is not None:
            self._store.enqueue_retry(
                context.authority_id,
                context.active_reference,
                error_name,
            )
        self._store.record_failure(
            context.authority_id,
            context.run_id,
            error_name,
            error_name,
            retryable=context.active_reference is not None,
        )
        self._store.finish_run(
            context.run_id,
            context.authority_id,
            RunOutcome(
                status=status,
                metrics=self._metrics(context),
                transport_mode=context.session.mode,
                failure_message=error_name,
            ),
        )

    def _metrics(
        self,
        context: _RunContext,
    ) -> RunMetrics:
        return RunMetrics(
            request_count=len(context.session.requested_urls),
            transferred_bytes=context.session.transferred_bytes,
            duration_ms=max(0, round((monotonic() - context.started) * 1000)),
            browser_time_ms=context.session.browser_time_ms,
            attachment_body_requests=self._attachment_body_requests(context),
            storage_growth_bytes=max(
                0,
                self._store.storage_bytes() - context.storage_before,
            ),
        )

    @staticmethod
    def _attachment_body_requests(context: _RunContext) -> int:
        published = Counter(_transport_url(url) for url in context.attachment_urls)
        requested = Counter(
            _transport_url(url) for url in context.session.requested_urls
        )
        retrieved = sum(min(count, requested[url]) for url, count in published.items())
        return context.session.attachment_body_requests + retrieved


def _transport_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
