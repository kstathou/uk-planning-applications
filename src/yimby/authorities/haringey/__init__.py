# Copyright (c) 2026 Kostas Stathoulopoulos

"""Haringey planning register adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.haringey.adapter import (
    HaringeyAdapter,
    HaringeyApplicationV1,
    HaringeyCheckpointV1,
)

HARINGEY_PACKAGE: AuthorityPackage[HaringeyApplicationV1, HaringeyCheckpointV1] = (
    AuthorityPackage(HaringeyAdapter(), HaringeyCheckpointV1)
)

__all__ = ["HARINGEY_PACKAGE"]
