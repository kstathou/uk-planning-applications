# Copyright (c) 2026 Kostas Stathoulopoulos

"""Local command-line product surface for the planning pilot."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from yimby.backup import (
    BackupTargetExistsError,
    BackupVerificationError,
    RestoreTargetExistsError,
    create_backup,
    default_backup_path,
    restore_backup,
)
from yimby.collection import Collector
from yimby.dashboard import dashboard_snapshot, search_dashboard
from yimby.doctor import run_doctor
from yimby.domain import ApplicationId, AuthorityId, DiscoveryWindow
from yimby.evidence import EvidenceStore
from yimby.exporting import ExportFormat, ExportProfile, export_records
from yimby.normalise import rebuild_normalised
from yimby.pilot_fixtures import FIXTURE_BUILDERS
from yimby.registry import AuthorityRegistry, pilot_registry
from yimby.store import SqliteStore

if TYPE_CHECKING:
    from collections.abc import Sequence


class LiveTransportUnavailableError(RuntimeError):
    """Live authority transport is intentionally deferred to unit four."""

    def __init__(self) -> None:
        """Provide the fixture-mode remediation."""
        super().__init__(
            "live transport is not available yet; rerun with --fixture for "
            "sanitised local data"
        )


def build_parser() -> argparse.ArgumentParser:
    """Build the complete documented command grammar."""
    registry = pilot_registry()
    authority_choices = ("all", *(str(value) for value in registry.ids()))
    parser = argparse.ArgumentParser(prog="yimby")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(".yimby"),
        help="local SQLite and evidence directory",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("authorities", help="list pilot authority manifests")

    bootstrap = commands.add_parser("bootstrap", help="collect an initial window")
    bootstrap.add_argument("--authority", required=True, choices=authority_choices)
    bootstrap.add_argument("--days", type=_positive_integer, default=30)
    bootstrap.add_argument("--include-open", action="store_true")
    bootstrap.add_argument("--fixture", action="store_true")

    sync = commands.add_parser("sync", help="refresh an authority")
    sync.add_argument("--authority", required=True, choices=authority_choices)
    sync.add_argument("--fixture", action="store_true")

    inspect = commands.add_parser("inspect", help="inspect a local application")
    inspect.add_argument("application_id")

    normalise = commands.add_parser(
        "normalise", help="rebuild semantics from retained native payloads"
    )
    normalise.add_argument("--rebuild", action="store_true", required=True)

    export = commands.add_parser("export", help="write deterministic local data")
    export.add_argument("--format", required=True, choices=tuple(ExportFormat))
    export.add_argument("--profile", required=True, choices=tuple(ExportProfile))
    export.add_argument("--output", type=Path)

    dashboard = commands.add_parser("dashboard", help="show local coverage metrics")
    dashboard.add_argument("--search")

    backup = commands.add_parser("backup", help="create a verified local backup")
    backup.add_argument("--output", type=Path)

    restore = commands.add_parser("restore", help="restore into a new directory")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--target", type=Path)

    commands.add_parser("doctor", help="check local operational health")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one command and return a console-script exit status."""
    args = build_parser().parse_args(argv)
    registry = pilot_registry()
    try:
        if args.command == "restore":
            target = args.target or args.data_dir.with_name(
                f"{args.data_dir.name}-restored"
            )
            restored = restore_backup(args.backup, target)
            _write_json({"restored": str(restored)})
            return 0
        store = _open_store(args.data_dir, registry)
        try:
            return _dispatch(args, registry, store)
        finally:
            store.close()
    except (
        BackupTargetExistsError,
        BackupVerificationError,
        KeyError,
        LiveTransportUnavailableError,
        RestoreTargetExistsError,
    ) as error:
        sys.stderr.write(f"error: {error}\n")
        return 2


def _dispatch(
    args: argparse.Namespace,
    registry: AuthorityRegistry,
    store: SqliteStore,
) -> int:
    exit_code = 0
    if args.command == "authorities":
        _write_json(
            [manifest.model_dump(mode="json") for manifest in registry.manifests()]
        )
    elif args.command in {"bootstrap", "sync"}:
        if not args.fixture:
            raise LiveTransportUnavailableError
        days = args.days if args.command == "bootstrap" else 30
        include_open = args.include_open if args.command == "bootstrap" else True
        reports = asyncio.run(
            _collect_fixtures(
                registry,
                store,
                args.authority,
                days,
                include_open=include_open,
            )
        )
        _write_json(reports)
    elif args.command == "inspect":
        view = store.application_view(ApplicationId(args.application_id))
        _write_json(view.model_dump(mode="json"))
    elif args.command == "normalise":
        normalisation_report = rebuild_normalised(store, registry)
        _write_json(normalisation_report.model_dump(mode="json"))
    elif args.command == "export":
        output_format = ExportFormat(args.format)
        profile = ExportProfile(args.profile)
        destination = args.output or (
            args.data_dir / "exports" / f"applications.{output_format}"
        )
        count = export_records(store, destination, output_format, profile)
        _write_json({"exported": count, "path": str(destination)})
    elif args.command == "dashboard":
        snapshot = dashboard_snapshot(store, registry)
        payload: dict[str, object] = {"snapshot": snapshot.model_dump(mode="json")}
        if args.search is not None:
            payload["search"] = [
                hit.model_dump(mode="json")
                for hit in search_dashboard(store, args.search)
            ]
        _write_json(payload)
    elif args.command == "backup":
        destination = args.output or default_backup_path(args.data_dir)
        result = create_backup(store, destination)
        _write_json({"backup": str(result)})
    else:
        doctor_report = run_doctor(
            store,
            args.data_dir,
            expected_authorities=len(registry.ids()),
        )
        _write_json(doctor_report.model_dump(mode="json"))
        exit_code = 0 if doctor_report.ok else 1
    return exit_code


async def _collect_fixtures(
    registry: AuthorityRegistry,
    store: SqliteStore,
    authority: str,
    days: int,
    *,
    include_open: bool,
) -> list[dict[str, object]]:
    end = datetime.now(UTC).date()
    window = DiscoveryWindow(
        start=end - timedelta(days=days),
        end=end,
        include_open=include_open,
    )
    selected = registry.ids() if authority == "all" else (AuthorityId(authority),)
    collector = Collector(registry, store)
    reports = []
    for authority_id in selected:
        report = await collector.collect(
            authority_id,
            window,
            FIXTURE_BUILDERS[authority_id](window),
        )
        reports.append(
            {
                "authority_id": str(authority_id),
                "applications": [str(value) for value in report.applications],
                "attachment_body_requests": report.attachment_body_requests,
            }
        )
    return reports


def _open_store(data_dir: Path, registry: AuthorityRegistry) -> SqliteStore:
    store = SqliteStore(
        data_dir / "yimby.sqlite3",
        EvidenceStore(data_dir / "evidence"),
    )
    store.register_authorities(registry.manifests())
    return store


def _positive_integer(value: str) -> int:
    number = int(value)
    if number <= 0:
        message = "must be greater than zero"
        raise argparse.ArgumentTypeError(message)
    return number


def _write_json(value: object) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
