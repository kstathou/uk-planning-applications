# Copyright (c) 2026 Kostas Stathoulopoulos

"""Camden API collection with offline compatibility for retained portal records."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from yimby.adapters import AuthorityPackage
from yimby.authorities.camden.adapter import (
    CamdenAdapter,
    CamdenApplicationV1,
)
from yimby.authorities.camden.discovery import CamdenCheckpointV1
from yimby.authorities.camden.open_data import (
    CamdenOpenDataAdapter,
    CamdenOpenDataApplicationV1,
    CamdenOpenDataCheckpointV1,
)
from yimby.domain import TransportMode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from yimby.domain import (
        CollectedObservation,
        DiscoveryWindow,
        DurableDiscoveryBatch,
        NormalisedObservation,
        RetainedNativeRecord,
        SourceReference,
        StoredCheckpoint,
    )
    from yimby.transport import PortalSession

LEGACY_CAMDEN_PACKAGE: AuthorityPackage[CamdenApplicationV1, CamdenCheckpointV1] = (
    AuthorityPackage(CamdenAdapter(), CamdenApplicationV1, CamdenCheckpointV1)
)


class CamdenPackage:
    """Use Socrata for live runs; retain fixture and offline rebuild compatibility."""

    def __init__(self) -> None:
        """Bind the API adapter and typed schemas."""
        self._api = AuthorityPackage(
            CamdenOpenDataAdapter(),
            CamdenOpenDataApplicationV1,
            CamdenOpenDataCheckpointV1,
        )
        self.manifest = self._api.manifest

    async def discover(
        self,
        session: PortalSession,
        window: DiscoveryWindow,
        checkpoint: StoredCheckpoint | None,
    ) -> AsyncIterator[DurableDiscoveryBatch]:
        """Retire portal cursors when entering the official API workflow."""
        if session.mode == TransportMode.FIXTURE:
            async for batch in LEGACY_CAMDEN_PACKAGE.discover(
                session, window, checkpoint
            ):
                yield batch
            return
        if (
            checkpoint is not None
            and json.loads(checkpoint.payload_json).get("kind") != "camden-open-data"
        ):
            checkpoint = None
        async for batch in self._api.discover(session, window, checkpoint):
            yield batch

    async def collect(
        self, session: PortalSession, reference: SourceReference
    ) -> CollectedObservation:
        """Collect through the API except for explicitly offline legacy fixtures."""
        package = (
            LEGACY_CAMDEN_PACKAGE
            if session.mode == TransportMode.FIXTURE
            else self._api
        )
        return await package.collect(session, reference)

    def rebuild(self, retained: RetainedNativeRecord) -> NormalisedObservation:
        """Rebuild either retained native schema without contacting a portal."""
        if retained.native_schema == "CamdenApplicationV1":
            return LEGACY_CAMDEN_PACKAGE.rebuild(retained)
        return self._api.rebuild(retained)


CAMDEN_PACKAGE = CamdenPackage()

__all__ = ["CAMDEN_PACKAGE"]
