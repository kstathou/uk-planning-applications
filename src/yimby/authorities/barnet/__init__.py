# Copyright (c) 2026 Kostas Stathoulopoulos

"""Barnet planning register adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.barnet.adapter import (
    BarnetAdapter,
    BarnetApplicationV1,
    BarnetCheckpointV1,
)

BARNET_PACKAGE: AuthorityPackage[BarnetApplicationV1, BarnetCheckpointV1] = (
    AuthorityPackage(BarnetAdapter(), BarnetCheckpointV1)
)

__all__ = ["BARNET_PACKAGE"]
