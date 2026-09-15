# Copyright (c) 2026 Kostas Stathoulopoulos

"""Blackburn with Darwen planning register adapter."""

from yimby.adapters import AuthorityPackage
from yimby.authorities.blackburn_with_darwen.adapter import (
    BlackburnWithDarwenAdapter,
    BlackburnWithDarwenApplicationV1,
    BlackburnWithDarwenCheckpointV1,
)

BLACKBURN_WITH_DARWEN_PACKAGE: AuthorityPackage[
    BlackburnWithDarwenApplicationV1, BlackburnWithDarwenCheckpointV1
] = AuthorityPackage(BlackburnWithDarwenAdapter(), BlackburnWithDarwenCheckpointV1)

__all__ = ["BLACKBURN_WITH_DARWEN_PACKAGE"]
