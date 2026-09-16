# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Collect and verify Camden's official open-data feed without a browser."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from yimby.authorities.camden import CamdenPackage
from yimby.authorities.camden.open_data import (
    DATASET_URL,
    CamdenOpenDataCheckpointV1,
    create_session,
)
from yimby.collection import Collector
from yimby.domain import AuthorityId, DiscoveryWindow
from yimby.evidence import EvidenceStore
from yimby.orchestration import ProcessLock
from yimby.registry import AuthorityRegistry
from yimby.store import SqliteStore

if TYPE_CHECKING:
    from collections.abc import Sequence


async def qualify(data_dir: Path, window: DiscoveryWindow) -> dict[str, object]:
    """Persist two complete feed reads and compare semantic state and evidence."""
    authority = AuthorityId("camden")
    store = SqliteStore(
        data_dir / "yimby.sqlite3", EvidenceStore(data_dir / "evidence")
    )
    passes = []
    states = []
    try:
        for _ in range(2):
            collector = Collector(AuthorityRegistry((CamdenPackage(),)), store)
            session = create_session()
            try:
                report = await collector.collect(authority, window, session)
                stored = store.discovery_state(authority).checkpoint
                if stored is None:
                    message = "Camden API checkpoint missing"
                    raise ValueError(message)
                checkpoint = CamdenOpenDataCheckpointV1.model_validate_json(
                    stored.payload_json
                )
                integrity = store.evidence_integrity(authority)
                if (
                    not checkpoint.complete
                    or integrity.issues
                    or store.database_integrity() != "ok"
                ):
                    message = "Camden API collection proof failed"
                    raise ValueError(message)
                passes.append(
                    {
                        "applications_collected": len(report.applications),
                        "source_applications": checkpoint.expected,
                        "enumerated": checkpoint.enumerated,
                        "source_last_uploaded": checkpoint.watermark,
                        "requests": len(session.requested_urls),
                        "response_bytes": session.transferred_bytes,
                        "attachment_body_requests": session.attachment_body_requests,
                        "evidence": integrity.model_dump(mode="json"),
                    }
                )
                states.append(store.authority_semantic_state(authority))
            finally:
                await session.aclose()
        return {
            "schema_version": 1,
            "source": DATASET_URL,
            "created_at": datetime.now(UTC).isoformat(),
            "scope": window.model_dump(mode="json"),
            "passes": passes,
            "immediate_refresh_unchanged": states[0] == states[1],
            "database_integrity": store.database_integrity(),
            "coverage": (
                "Published application metadata only; "
                "no document index or public comment text"
            ),
            "weekly_cycles": "pending",
        }
    finally:
        store.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Require explicit live opt-in and preserve existing resumable storage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--include-open", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_live:
        parser.error("--confirm-live is required")
    if args.data_dir.exists() and any(args.data_dir.iterdir()) and not args.resume:
        parser.error("--resume is required for an existing nonempty data directory")
    window = DiscoveryWindow(
        start=args.start, end=args.end, include_open=args.include_open
    )
    with ProcessLock(args.data_dir / "qualification.lock"):
        receipt = asyncio.run(qualify(args.data_dir, window))
        destination = args.data_dir / "camden-open-data-qualification.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
