"""Shared test fixtures and helpers."""

from __future__ import annotations

from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def fixture_path(name: str) -> Path:
    """Return the absolute path to a test fixture by filename.

    Named ``fixture_path`` (not ``fixture``) so it doesn't read like a pytest
    fixture factory at a glance. Tests should call this rather than constructing
    fixture paths by hand — keeps the fixture directory location in one place.
    """
    return FIXTURES_DIR / name
