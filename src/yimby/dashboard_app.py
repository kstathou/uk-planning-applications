# Copyright (c) 2026 Kostas Stathoulopoulos

"""Local Streamlit dashboard backed only by the SQLite product store."""

from __future__ import annotations

import argparse
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from yimby.dashboard import dashboard_snapshot, search_dashboard
from yimby.evidence import EvidenceStore
from yimby.registry import pilot_registry
from yimby.store import SqliteStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from yimby.domain import ApplicationSearchHit, DashboardSnapshot


class DashboardSurface(Protocol):
    """Streamlit calls used by the renderer and its deterministic fake."""

    def title(self, body: str) -> object:
        """Render a title."""

    def subheader(self, body: str) -> object:
        """Render a section title."""

    def metric(self, label: str, value: object) -> object:
        """Render one metric."""

    def dataframe(self, data: object, *, use_container_width: bool) -> object:
        """Render tabular data."""

    def text_input(self, label: str) -> str:
        """Read a local search query."""

    def map(self, data: object) -> object:
        """Render WGS84 locations."""


def render_dashboard(
    surface: DashboardSurface,
    snapshot: DashboardSnapshot,
    hits: tuple[ApplicationSearchHit, ...],
) -> None:
    """Render operational truth without performing persistence queries."""
    surface.title("YIMBY planning collection")
    surface.metric(
        "Fixture implementation",
        f"{snapshot.coverage_implemented}/{snapshot.coverage_denominator}",
    )
    surface.metric(
        "Live ready",
        f"{snapshot.live_ready}/{snapshot.live_readiness_denominator}",
    )
    surface.metric("Unmapped applications", snapshot.unmapped_count)
    surface.metric("Requests", snapshot.request_count)
    surface.metric("Transferred bytes", snapshot.transferred_bytes)
    surface.metric("Duration (ms)", snapshot.duration_ms)
    surface.metric("Browser time (ms)", snapshot.browser_time_ms)
    surface.metric("Storage growth (bytes)", snapshot.storage_growth_bytes)
    surface.subheader("Authorities")
    surface.dataframe(
        [row.model_dump(mode="json") for row in snapshot.authorities],
        use_container_width=True,
    )
    surface.subheader("Application search")
    surface.dataframe(
        [hit.model_dump(mode="json") for hit in hits],
        use_container_width=True,
    )
    locations = [
        {
            "lat": hit.location.wgs84.latitude,
            "lon": hit.location.wgs84.longitude,
        }
        for hit in hits
        if hit.location is not None
    ]
    if locations:
        surface.map(locations)


def run_dashboard(data_dir: Path, surface: DashboardSurface) -> None:
    """Query the local store and render one Streamlit refresh."""
    registry = pilot_registry()
    store = SqliteStore(
        data_dir / "yimby.sqlite3",
        EvidenceStore(data_dir / "evidence"),
    )
    try:
        store.register_authorities(registry.manifests())
        query = surface.text_input("Search applications")
        hits = search_dashboard(store, query) if query else ()
        render_dashboard(surface, dashboard_snapshot(store, registry), hits)
    finally:
        store.close()


def launch_dashboard(data_dir: Path) -> int:
    """Launch the actual local Streamlit process without a shell."""
    command = (
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(Path(__file__).resolve()),
        "--",
        "--data-dir",
        str(data_dir),
    )
    return subprocess.run(command, check=False).returncode  # noqa: S603


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Streamlit script entry point."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    surface = cast("DashboardSurface", import_module("streamlit"))
    run_dashboard(args.data_dir, surface)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
