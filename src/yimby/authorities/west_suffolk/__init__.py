# Copyright (c) 2026 Kostas Stathoulopoulos

"""West Suffolk planning register adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.west_suffolk.adapter import (
    WestSuffolkAdapter,
    WestSuffolkApplicationV1,
    WestSuffolkCheckpointV1,
)

WEST_SUFFOLK_PACKAGE: AuthorityPackage[
    WestSuffolkApplicationV1, WestSuffolkCheckpointV1
] = AuthorityPackage(
    WestSuffolkAdapter(), WestSuffolkApplicationV1, WestSuffolkCheckpointV1
)

__all__ = ["WEST_SUFFOLK_PACKAGE"]
