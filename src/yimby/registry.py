# Copyright (c) 2026 Kostas Stathoulopoulos

"""Authority package registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from yimby.authorities.barnet import BARNET_PACKAGE

if TYPE_CHECKING:
    from collections.abc import Iterable

    from yimby.adapters import RunnableAuthority
    from yimby.domain import AuthorityId


class DuplicateAuthorityError(ValueError):
    """Two packages claim the same authority identifier."""

    def __init__(self) -> None:
        """Describe the conflicting ownership claim."""
        super().__init__("duplicate authority id")


class AuthorityRegistry:
    """Resolve each authority to one independently owned package."""

    def __init__(self, packages: Iterable[RunnableAuthority]) -> None:
        """Index packages by their stable authority identifier."""
        package_list = tuple(packages)
        self._packages = {package.manifest.id: package for package in package_list}
        if len(self._packages) != len(package_list):
            raise DuplicateAuthorityError

    def get(self, authority_id: AuthorityId) -> RunnableAuthority:
        """Return the registered authority package."""
        return self._packages[authority_id]

    def ids(self) -> tuple[AuthorityId, ...]:
        """Return registered authority identifiers in stable order."""
        return tuple(sorted(self._packages))


def barnet_registry() -> AuthorityRegistry:
    """Return the unit-one registry containing Barnet."""
    return AuthorityRegistry((BARNET_PACKAGE,))
