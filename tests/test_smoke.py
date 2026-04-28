"""Phase 0 sanity check that the package imports and reports a version.

Removed in PR 1 — by then the foundation tests in test_file_info.py and
test_analyzer.py exercise the import path far more thoroughly.
"""

from __future__ import annotations


def test_import() -> None:
    import pixel_probe

    assert pixel_probe.__version__
