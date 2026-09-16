# Copyright (c) 2026 Kostas Stathoulopoulos

"""Failure-isolated authority orchestration and process overlap locking."""

from __future__ import annotations

import asyncio
import fcntl
import os
import stat
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
    """Kernel-owned advisory lock with a diagnostic PID file."""

    def __init__(self, path: Path) -> None:
        """Configure the persistent file used for advisory locking."""
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> Self:
        """Acquire the kernel lock and publish this process identifier."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self._path,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as error:
            raise _unsafe_lock_error() from error
        try:
            mode = os.fstat(descriptor).st_mode
        except BaseException:
            os.close(descriptor)
            raise
        if not stat.S_ISREG(mode):
            os.close(descriptor)
            raise _unsafe_lock_error()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            owner = _read_owner(descriptor)
            os.close(descriptor)
            if owner is not None:
                raise _owned_lock_error(owner) from None
            raise _concurrent_lock_error() from error
        except BaseException:
            os.close(descriptor)
            raise
        try:
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, f"{os.getpid()}\n".encode())
            os.fsync(descriptor)
        except BaseException:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release a lock owned by this context."""
        descriptor = self._descriptor
        if descriptor is None:
            return
        try:
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            self._descriptor = None


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


def _read_owner(descriptor: int) -> int | None:
    """Read only a valid positive PID from a contended lock file."""
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        owner = int(os.read(descriptor, 64).decode().strip())
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return owner if owner > 0 else None


def _owned_lock_error(owner: int) -> CollectionAlreadyRunningError:
    return CollectionAlreadyRunningError(f"collection already running with pid {owner}")


def _concurrent_lock_error() -> CollectionAlreadyRunningError:
    return CollectionAlreadyRunningError("collection lock was acquired concurrently")


def _unsafe_lock_error() -> CollectionAlreadyRunningError:
    return CollectionAlreadyRunningError("collection lock path is not a regular file")
