# Copyright (c) 2026 Kostas Stathoulopoulos

"""GUI-independent dashboard queries and completeness denominators."""

from __future__ import annotations

from typing import TYPE_CHECKING

from yimby.domain import (
    ApplicationSearchHit,
    DashboardAuthority,
    DashboardSnapshot,
    LiveReadiness,
)

if TYPE_CHECKING:
    from yimby.registry import AuthorityRegistry
    from yimby.store import SqliteStore


def dashboard_snapshot(
    store: SqliteStore,
    registry: AuthorityRegistry,
) -> DashboardSnapshot:
    """Build the complete local operational dashboard model."""
    states = store.authority_states()
    authorities = tuple(
        DashboardAuthority(
            authority_id=state.manifest.id,
            name=state.manifest.name,
            implementation_status=state.implementation_status,
            live_readiness=state.manifest.live_status.readiness,
            live_reason=state.manifest.live_status.reason,
            live_evidence=state.manifest.live_status.evidence,
            transport_mode=state.transport_mode,
            freshness_days=state.freshness_days,
            failures=state.failure_count,
            backlog=state.backlog_count,
        )
        for state in states
    )
    metrics = store.metrics_totals()
    return DashboardSnapshot(
        coverage_implemented=len(authorities),
        coverage_denominator=len(registry.ids()),
        live_ready=sum(
            state.manifest.live_status.readiness == LiveReadiness.LIVE_READY
            for state in states
        ),
        live_readiness_denominator=len(registry.ids()),
        authorities=authorities,
        request_count=metrics.request_count,
        transferred_bytes=metrics.transferred_bytes,
        duration_ms=metrics.duration_ms,
        browser_time_ms=metrics.browser_time_ms,
        storage_growth_bytes=metrics.storage_growth_bytes,
        application_count=store.application_count(),
        observed_change_count=store.observed_change_count(),
        unmapped_count=store.unmapped_count(),
    )


def search_dashboard(
    store: SqliteStore,
    query: str,
) -> tuple[ApplicationSearchHit, ...]:
    """Search application reference, proposal, and address for dashboard use."""
    return tuple(
        ApplicationSearchHit(
            application_id=view.application.id,
            authority_id=view.application.authority_id,
            reference=view.application.reference,
            proposal=view.application.proposal,
            address=view.metadata.address,
            location=view.metadata.location,
        )
        for view in store.search_applications(query)
    )
