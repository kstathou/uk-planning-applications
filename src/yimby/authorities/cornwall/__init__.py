# Copyright (c) 2026 Kostas Stathoulopoulos

"""Cornwall planning register adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.cornwall.adapter import (
    CornwallAdapter,
    CornwallApplicationV1,
    CornwallCheckpointV1,
)

CORNWALL_PACKAGE: AuthorityPackage[CornwallApplicationV1, CornwallCheckpointV1] = (
    AuthorityPackage(CornwallAdapter(), CornwallApplicationV1, CornwallCheckpointV1)
)

__all__ = ["CORNWALL_PACKAGE"]
