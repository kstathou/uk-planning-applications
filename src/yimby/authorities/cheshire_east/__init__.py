# Copyright (c) 2026 Kostas Stathoulopoulos

"""Cheshire East planning register adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.cheshire_east.adapter import (
    CheshireEastAdapter,
    CheshireEastApplicationV1,
    CheshireEastCheckpointV1,
)

CHESHIRE_EAST_PACKAGE: AuthorityPackage[
    CheshireEastApplicationV1, CheshireEastCheckpointV1
] = AuthorityPackage(
    CheshireEastAdapter(), CheshireEastApplicationV1, CheshireEastCheckpointV1
)

__all__ = ["CHESHIRE_EAST_PACKAGE"]
