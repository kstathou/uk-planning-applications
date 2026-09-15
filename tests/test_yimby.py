# Copyright (c) 2026 Kostas Stathoulopoulos

"""Tests for the public yimby package API."""

from yimby import hello


def test_hello() -> None:
    """The starter API returns its stable greeting."""
    assert hello() == "Hello from yimby!"
