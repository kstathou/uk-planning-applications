# Copyright (c) 2026 Kostas Stathoulopoulos

"""Durham planning register adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.durham.adapter import (
    DurhamAdapter,
    DurhamApplicationV1,
    DurhamCheckpointV1,
)

DURHAM_PACKAGE: AuthorityPackage[DurhamApplicationV1, DurhamCheckpointV1] = (
    AuthorityPackage(DurhamAdapter(), DurhamApplicationV1, DurhamCheckpointV1)
)

__all__ = ["DURHAM_PACKAGE"]
