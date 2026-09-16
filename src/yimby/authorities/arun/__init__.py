# Copyright (c) 2026 Kostas Stathoulopoulos

"""Arun planning register adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.arun.adapter import (
    ArunAdapter,
    ArunApplicationV1,
    ArunCheckpointV1,
)

ARUN_PACKAGE: AuthorityPackage[ArunApplicationV1, ArunCheckpointV1] = AuthorityPackage(
    ArunAdapter(), ArunApplicationV1, ArunCheckpointV1
)

__all__ = ["ARUN_PACKAGE"]
