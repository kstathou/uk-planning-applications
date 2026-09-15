# Copyright (c) 2026 Kostas Stathoulopoulos

"""Offline normalisation from retained authority-native payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from yimby.domain import NormalisationReport

if TYPE_CHECKING:
    from yimby.registry import AuthorityRegistry
    from yimby.store import SqliteStore


def rebuild_normalised(
    store: SqliteStore,
    registry: AuthorityRegistry,
) -> NormalisationReport:
    """Rebuild every retained native record without creating a transport."""
    rebuilt = 0
    for retained in store.retained_native_records():
        package = registry.get(retained.authority_id)
        normalised = package.rebuild(retained)
        store.commit_rebuild(retained.application_id, normalised)
        rebuilt += 1
    return NormalisationReport(rebuilt=rebuilt, transport_requests=0)
