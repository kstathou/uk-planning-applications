# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: INP001, T201

"""Collect and verify Camden's official open-data feed without a browser."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import parse_qsl, urlsplit

from yimby.authorities.camden import CamdenPackage
from yimby.authorities.camden.open_data import (
    DATASET_URL,
    CamdenOpenDataCheckpointV1,
    create_session,
    page_parameters,
    scope_filter,
    summary_parameters,
)
from yimby.collection import Collector
from yimby.domain import AuthorityId, DiscoveryWindow
from yimby.evidence import EvidenceStore
from yimby.orchestration import ProcessLock
from yimby.registry import AuthorityRegistry
from yimby.store import SqliteStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from yimby.store import RetainedDiscoveryEvidenceRegistration


_REQUESTS_PER_QUERY = 2


def _fail(reason: str) -> NoReturn:
    raise ValueError(reason)


def _request_parameters(
    item: RetainedDiscoveryEvidenceRegistration,
) -> dict[str, str]:
    if (
        item.page != 1
        or item.response_url != DATASET_URL
        or item.request_method != "GET"
        or item.request_form != ()
        or item.request_url is None
    ):
        _fail("Camden discovery evidence subject is invalid")
    parsed = urlsplit(item.request_url)
    if f"{parsed.scheme}://{parsed.netloc}{parsed.path}" != DATASET_URL or (
        parsed.fragment
    ):
        _fail("Camden discovery evidence URL is invalid")
    return dict(parse_qsl(parsed.query))


def _query_cursor(query_key: str) -> int:
    prefix = "socrata:after:"
    if not query_key.startswith(prefix):
        _fail("Camden discovery evidence query key is invalid")
    try:
        return int(query_key.removeprefix(prefix))
    except ValueError:
        _fail("Camden discovery evidence cursor is invalid")


def _expected_request_parameters(
    window: DiscoveryWindow,
    cursor: int,
) -> tuple[dict[str, str], dict[str, str]]:
    where = scope_filter(window)
    return (
        summary_parameters(where),
        page_parameters(where, cursor),
    )


def _validate_discovery_registrations(
    registrations: tuple[RetainedDiscoveryEvidenceRegistration, ...],
    window: DiscoveryWindow,
) -> int:
    if not registrations:
        _fail("Camden discovery evidence is missing")
    run_ids = {item.run_id for item in registrations}
    if len(run_ids) != 1:
        _fail("Camden discovery evidence spans multiple runs")
    groups: dict[str, list[RetainedDiscoveryEvidenceRegistration]] = {}
    for item in registrations:
        groups.setdefault(item.query_key, []).append(item)
    cursors: set[int] = set()
    for query_key, items in groups.items():
        cursor = _query_cursor(query_key)
        cursors.add(cursor)
        request_parameters = [_request_parameters(item) for item in items]
        if len(request_parameters) != _REQUESTS_PER_QUERY or any(
            subject not in request_parameters
            for subject in _expected_request_parameters(window, cursor)
        ):
            _fail("Camden discovery evidence request contract is invalid")
    if -1 not in cursors:
        _fail("Camden discovery evidence does not start at the scope boundary")
    return len(groups)


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
            previous_run_ids = {
                item.run_id
                for item in store.evidence_registration_audit(
                    authority
                ).discovery_registrations
            }
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
                registration_audit = store.evidence_registration_audit(authority)
                current_registrations = tuple(
                    item
                    for item in registration_audit.discovery_registrations
                    if item.run_id not in previous_run_ids
                )
                discovery_query_pages = _validate_discovery_registrations(
                    current_registrations,
                    window,
                )
                if (
                    not checkpoint.complete
                    or integrity.issues
                    or registration_audit.missing_digests
                    or store.database_integrity() != "ok"
                ):
                    message = "Camden API collection proof failed"
                    raise ValueError(message)
                application_count = len(report.applications)
                if not (
                    application_count == checkpoint.expected == checkpoint.enumerated
                    and report.attachment_body_requests == 0
                ):
                    message = "Camden API collection counts or attachment policy failed"
                    raise ValueError(message)
                passes.append(
                    {
                        "applications_collected": application_count,
                        "source_applications": checkpoint.expected,
                        "enumerated": checkpoint.enumerated,
                        "source_last_uploaded": checkpoint.watermark,
                        "requests": len(session.requested_urls),
                        "response_bytes": session.transferred_bytes,
                        "attachment_body_requests": report.attachment_body_requests,
                        "discovery_evidence_registrations": len(current_registrations),
                        "discovery_query_pages": discovery_query_pages,
                        "evidence": integrity.model_dump(mode="json"),
                    }
                )
                states.append(store.authority_semantic_state(authority))
            finally:
                await session.aclose()
        if states[0] != states[1]:
            message = "Camden API immediate refresh changed semantic state"
            raise ValueError(message)
        return {
            "schema_version": 2,
            "source": DATASET_URL,
            "created_at": datetime.now(UTC).isoformat(),
            "scope": window.model_dump(mode="json"),
            "passes": passes,
            "immediate_refresh_unchanged": True,
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
