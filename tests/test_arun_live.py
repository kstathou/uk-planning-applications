# Copyright (c) 2026 Kostas Stathoulopoulos
# ruff: noqa: D103, PLR2004, SLF001

"""Arun live discovery and qualification contracts."""

from datetime import date, timedelta
from itertools import pairwise

import yimby.authorities.arun.adapter as arun


def test_arun_canonical_query_plan_is_complete_and_non_overlapping() -> None:
    scope = arun.ArunDiscoveryScope(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=True,
    )

    plan = arun._canonical_query_plan(scope)

    assert len(plan) == 60
    assert isinstance(plan[0], arun.ArunReceivedQuery)
    assert plan[0].key == "received|2026-08-18|2026-09-16"
    assert isinstance(plan[1], arun.ArunDecidedQuery)
    assert plan[1].key == "decided|2026-08-18|2026-09-16"

    open_queries = plan[2:]
    assert all(isinstance(query, arun.ArunOpenReceivedQuery) for query in open_queries)
    assert open_queries[0].key == "open-received|1948-01-01|1999-12-31"
    assert open_queries[1].key == "open-received|2000-01-01|2000-12-31"
    assert open_queries[24].key == "open-received|2023-01-01|2023-12-31"
    assert open_queries[25].key == "open-received|2024-01-01|2024-01-31"
    assert open_queries[-1].key == "open-received|2026-09-01|2026-09-16"
    assert all(
        left.end + timedelta(days=1) == right.start
        for left, right in pairwise(open_queries)
    )


def test_arun_plan_without_open_contains_only_the_bounded_first_pass() -> None:
    scope = arun.ArunDiscoveryScope(
        start=date(2026, 8, 18),
        end=date(2026, 9, 16),
        include_open=False,
    )

    plan = arun._canonical_query_plan(scope)

    assert [query.kind for query in plan] == ["received", "decided"]
