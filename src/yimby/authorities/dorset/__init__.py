# Copyright (c) 2026 Kostas Stathoulopoulos

"""Dorset planning register adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.dorset.adapter import (
    DorsetAdapter,
    DorsetApplicationV1,
    DorsetCheckpointV1,
)

DORSET_PACKAGE: AuthorityPackage[DorsetApplicationV1, DorsetCheckpointV1] = (
    AuthorityPackage(DorsetAdapter(), DorsetCheckpointV1)
)

__all__ = ["DORSET_PACKAGE"]
