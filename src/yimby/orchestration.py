# Copyright (c) 2026 Kostas Stathoulopoulos

"""Failure-isolated authority orchestration and process overlap locking."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Self

from yimby.browser_transport import BrowserWorker, PlaywrightPortalSession
from yimby.collection import Collector
from yimby.domain import (
    AuthorityCollectionResult,
    AuthorityCollectionStatus,
    AuthorityId,
    DiscoveryWindow,
    LiveReadiness,
    LiveTransportKind,
)
from yimby.http_transport import HostRateLimiter, HttpxPortalSession
from yimby.transport import PortalSession

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

    from yimby.registry import AuthorityRegistry
    from yimby.store import SqliteStore

SessionFactory = Callable[[AuthorityId], Awaitable[PortalSession]]
_MAX_AUTHORITY_CONCURRENCY = 4


class CollectionAlreadyRunningError(RuntimeError):
    """Another process owns the collection lock."""


class ProcessLock:
    """Exclusive PID lock that recovers from a conclusively stale owner."""

    def __init__(
        self,
        path: Path,
        *,
        process_alive: Callable[[int], bool] | None = None,
    ) -> None:
        """Configure a PID file and injectable liveness probe."""
        self._path = path
        self._process_alive = process_alive or _process_alive
        self._held = False

    def __enter__(self) -> Self:
        """Acquire the lock, replacing only a stale PID file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._create()
        except FileExistsError:
            owner = self._read_owner()
            if owner is not None and self._process_alive(owner):
                raise _owned_lock_error(owner) from None
            self._path.unlink(missing_ok=True)
            try:
                self._create()
            except FileExistsError as error:
                raise _concurrent_lock_error() from error
        self._held = True
        return self

    def _create(self) -> None:
        descriptor = os.open(
            self._path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode())
        finally:
            os.close(descriptor)

    def _read_owner(self) -> int | None:
        try:
            value = self._path.read_text().strip()
            owner = int(value)
        except (OSError, ValueError):
            return None
        return owner if owner > 0 else None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release a lock owned by this context."""
        if self._held:
            self._path.unlink(missing_ok=True)
            self._held = False


class LiveSessionFactory:
    """Create live transports while sharing host and browser limits."""

    def __init__(self, registry: AuthorityRegistry) -> None:
        """Share throttles across sessions created for one orchestration."""
        self._registry = registry
        self._http_limiter = HostRateLimiter()
        self._browser_worker = BrowserWorker()

    async def __call__(self, authority_id: AuthorityId) -> PortalSession:
        """Create the transport named by an authority's live status."""
        transport = self._registry.manifest(authority_id).live_status.transport
        if transport == LiveTransportKind.HTTP:
            return HttpxPortalSession(limiter=self._http_limiter)
        if transport == LiveTransportKind.BROWSER:
            return await PlaywrightPortalSession.create(worker=self._browser_worker)
        msg = f"{authority_id} has no live transport"
        raise ValueError(msg)


class CollectionOrchestrator:
    """Collect up to four authorities with one event-loop SQLite writer."""

    def __init__(
        self,
        registry: AuthorityRegistry,
        store: SqliteStore,
        *,
        max_concurrency: int = 4,
    ) -> None:
        """Bind the registry to its one writer and cap concurrency."""
        if not 1 <= max_concurrency <= _MAX_AUTHORITY_CONCURRENCY:
            msg = "max_concurrency must be between one and four"
            raise ValueError(msg)
        self._registry = registry
        self._collector = Collector(registry, store)
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def collect(
        self,
        authority_ids: Iterable[AuthorityId],
        window: DiscoveryWindow,
        session_factory: SessionFactory,
        *,
        fixture: bool,
    ) -> tuple[AuthorityCollectionResult, ...]:
        """Return one result per authority without cancelling its peers."""
        return tuple(
            await asyncio.gather(
                *(
                    self._collect_one(
                        authority_id,
                        window,
                        session_factory,
                        fixture=fixture,
                    )
                    for authority_id in authority_ids
                )
            )
        )

    async def _collect_one(
        self,
        authority_id: AuthorityId,
        window: DiscoveryWindow,
        session_factory: SessionFactory,
        *,
        fixture: bool,
    ) -> AuthorityCollectionResult:
        manifest = self._registry.manifest(authority_id)
        if not fixture and manifest.live_status.readiness != LiveReadiness.LIVE_READY:
            return AuthorityCollectionResult(
                authority_id=authority_id,
                status=AuthorityCollectionStatus.UNAVAILABLE,
                failure_code="LiveTransportUnavailable",
                message=manifest.live_status.reason,
            )
        async with self._semaphore:
            session: PortalSession | None = None
            try:
                session = await session_factory(authority_id)
                report = await self._collector.collect(authority_id, window, session)
                result = AuthorityCollectionResult(
                    authority_id=authority_id,
                    status=AuthorityCollectionStatus.SUCCEEDED,
                    applications=report.applications,
                    attachment_body_requests=report.attachment_body_requests,
                )
            except asyncio.CancelledError:
                if session is not None:
                    await session.aclose()
                raise
            except Exception as error:  # noqa: BLE001
                error_name = type(error).__name__
                result = AuthorityCollectionResult(
                    authority_id=authority_id,
                    status=AuthorityCollectionStatus.FAILED,
                    failure_code=error_name,
                    message=error_name,
                )
            if session is not None:
                try:
                    await session.aclose()
                except Exception as error:  # noqa: BLE001
                    error_name = type(error).__name__
                    return AuthorityCollectionResult(
                        authority_id=authority_id,
                        status=AuthorityCollectionStatus.FAILED,
                        failure_code=error_name,
                        message=error_name,
                    )
            return result


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _owned_lock_error(owner: int) -> CollectionAlreadyRunningError:
    return CollectionAlreadyRunningError(f"collection already running with pid {owner}")


def _concurrent_lock_error() -> CollectionAlreadyRunningError:
    return CollectionAlreadyRunningError("collection lock was acquired concurrently")
