"""Regenerate the small fixture images used by the test suite.

Output is **deterministic** — running this script twice on the same Pillow
version produces byte-identical files. That property lets tests assert
against hardcoded SHA-256 values.

Usage:

.. code-block:: bash

    make fixtures
    # equivalent to:  python scripts/build_fixtures.py

Run this script after a Pillow major version bump if the SHA assertions
in ``tests/test_file_info.py`` start failing.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from pathlib import Path

from PIL import Image

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_tiny_jpeg(out: Path) -> None:
    """1x1 black JPEG, ~600 bytes."""
    img = Image.new("RGB", (1, 1), color=(0, 0, 0))
    img.save(out, format="JPEG", quality=50, optimize=True)


def _build_tiny_png(out: Path) -> None:
    """1x1 transparent PNG, ~70 bytes."""
    img = Image.new("RGBA", (1, 1), color=(0, 0, 0, 0))
    img.save(out, format="PNG", optimize=True)


def _build_not_an_image(out: Path) -> None:
    """A plain text file used to exercise the not-an-image warning path."""
    out.write_bytes(b"This is not an image file.\n")


def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    builders: list[tuple[str, Callable[[Path], None]]] = [
        ("tiny.jpg", _build_tiny_jpeg),
        ("tiny.png", _build_tiny_png),
        ("not_an_image.txt", _build_not_an_image),
    ]
    print(f"Writing fixtures to {FIXTURES_DIR}")
    for name, build in builders:
        path = FIXTURES_DIR / name
        build(path)
        size = path.stat().st_size
        digest = _sha256(path)
        print(f"  {name:<20}  {size:>5} bytes  sha256={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
