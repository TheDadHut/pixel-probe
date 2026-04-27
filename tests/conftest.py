"""Shared test fixtures and helpers."""

from __future__ import annotations

from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Path:
    """Return the absolute path to a test fixture by filename.

    Tests should call this rather than constructing fixture paths by hand —
    keeps the fixture directory location in one place.
    """
    return FIXTURES_DIR / name
