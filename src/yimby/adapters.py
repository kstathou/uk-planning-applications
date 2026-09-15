# Copyright (c) 2026 Kostas Stathoulopoulos

"""Typed authority adapter contract and runtime erasure."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

from yimby.domain import (
    AuthorityManifest,
    CollectedObservation,
    DiscoveryBatch,
    DiscoveryWindow,
    DurableDiscoveryBatch,
    NativeSnapshot,
    NormalisedObservation,
    SourceReference,
    StoredCheckpoint,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.transport import PortalSession


class AuthorityAdapter[NativeT: BaseModel, CheckpointT: BaseModel](Protocol):
    """Contributor contract implemented once per authority."""

    manifest: AuthorityManifest

    def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: CheckpointT | None,
    ) -> AsyncIterator[DiscoveryBatch[CheckpointT]]:
        """Yield references with resumable progress."""
        ...

    async def fetch(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> NativeSnapshot[NativeT]:
        """Fetch one authority-native application."""
        ...

    def normalise(
        self,
        snapshot: NativeSnapshot[NativeT],
    ) -> NormalisedObservation:
        """Convert an authority-native record into the common model."""
        ...


class RunnableAuthority(Protocol):
    """Type-erased authority operations used by orchestration."""

    manifest: AuthorityManifest

    def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: StoredCheckpoint | None,
    ) -> AsyncIterator[DurableDiscoveryBatch]:
        """Yield persistence-ready discovery batches."""
        ...

    async def collect(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> CollectedObservation:
        """Fetch and normalise one application without losing native data."""
        ...


class AuthorityPackage[NativeT: BaseModel, CheckpointT: BaseModel]:
    """Preserve native type relationships behind the runtime interface."""

    def __init__(
        self,
        adapter: AuthorityAdapter[NativeT, CheckpointT],
        checkpoint_model: type[CheckpointT],
    ) -> None:
        """Bind one adapter to its checkpoint model."""
        self.manifest = adapter.manifest
        self._adapter = adapter
        self._checkpoint_model = checkpoint_model

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: StoredCheckpoint | None,
    ) -> AsyncIterator[DurableDiscoveryBatch]:
        """Decode and encode the authority checkpoint at one boundary."""
        native_checkpoint = (
            None
            if checkpoint is None
            else self._checkpoint_model.model_validate_json(checkpoint.payload_json)
        )
        async for batch in self._adapter.discover(
            session,
            window,
            native_checkpoint,
        ):
            yield DurableDiscoveryBatch(
                references=batch.references,
                next_checkpoint=StoredCheckpoint(
                    schema_version=1,
                    payload_json=batch.next_checkpoint.model_dump_json(),
                ),
                complete=batch.complete,
            )

    async def collect(
        self,
        session: PortalSession,
        reference: SourceReference,
    ) -> CollectedObservation:
        """Keep the native model paired with its normaliser."""
        snapshot = await self._adapter.fetch(session, reference)
        return CollectedObservation(
            native_schema=type(snapshot.payload).__name__,
            native_json=snapshot.payload.model_dump_json(),
            normalised=self._adapter.normalise(snapshot),
            evidence=snapshot.evidence,
            observed_at=snapshot.observed_at,
        )
