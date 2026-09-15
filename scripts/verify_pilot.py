# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001

"""Run the current pilot registry against sanitised fixtures."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from yimby import AuthorityId, Collector, DiscoveryWindow, barnet_registry
from yimby.authorities.barnet.fixtures import fixture_session
from yimby.evidence import EvidenceStore
from yimby.store import SqliteStore

LOGGER = logging.getLogger(__name__)


class AttachmentVerificationError(RuntimeError):
    """Fixture verification observed an attachment body request."""


async def verify() -> None:
    """Collect Barnet and fail if any attachment body was requested."""
    window = DiscoveryWindow(
        start=date(2026, 8, 16),
        end=date(2026, 9, 15),
    )
    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = SqliteStore(root / "yimby.sqlite3", EvidenceStore(root / "evidence"))
        report = await Collector(barnet_registry(), store).collect(
            AuthorityId("barnet"),
            window,
            fixture_session(window),
        )
        store.close()
    if report.attachment_body_requests != 0:
        raise AttachmentVerificationError
    LOGGER.info("barnet fixture verified")


if __name__ == "__main__":
    asyncio.run(verify())
