"""Regenerate the small fixture images used by the test suite.

Output is **deterministic** — running this script twice on the same Pillow
version produces byte-identical files. That property lets tests assert
against hardcoded SHA-256 values.

Usage:

.. code-block:: bash

    make fixtures
    # equivalent to:  python scripts/build_fixtures.py

Run this script if the SHA assertions in ``tests/test_file_info.py`` start
failing — typically after a Pillow upgrade (any version) that shifts the
encoder's output bytes, even on minor / patch releases.

EXIF authoring uses Pillow's native :class:`PIL.Image.Exif` API rather than
a third-party library — keeps the dev-dep graph minimal and means we
exercise the same write path Pillow consumers use in production.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from pathlib import Path

from PIL import Image
from PIL.ExifTags import GPS, Base
from PIL.TiffImagePlugin import IFDRational

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


def _build_exif_rich_jpeg(out: Path) -> None:
    """100x100 JPEG with rich EXIF: make/model/exposure/ISO/focal length/date,
    plus a deliberately oversized 100-byte MakerNote that exercises the
    bytes-summarization gate in _normalize.
    """
    img = Image.new("RGB", (100, 100), color=(50, 100, 150))

    exif = Image.Exif()
    # Main IFD tags.
    exif[Base.Make.value] = "TestCorp"
    exif[Base.Model.value] = "TestModel X1"
    exif[Base.Software.value] = "pixel-probe-fixtures"
    exif[Base.DateTime.value] = "2026:01:15 14:30:00"

    # Exif sub-IFD tags — accessed by getting/creating the sub-IFD via its
    # pointer tag, then mutating it in place. Pillow returns a live view.
    exif_ifd = exif.get_ifd(Base.ExifOffset.value)
    exif_ifd[Base.ExposureTime.value] = IFDRational(1, 250)  # 1/250 s
    exif_ifd[Base.FNumber.value] = IFDRational(28, 10)  # f/2.8
    exif_ifd[Base.ISOSpeedRatings.value] = 400
    exif_ifd[Base.FocalLength.value] = IFDRational(50, 1)  # 50 mm
    exif_ifd[Base.DateTimeOriginal.value] = "2026:01:15 14:30:00"
    # 100 bytes of "vendor-specific" binary; beyond _MAX_BYTES_INLINE (64).
    exif_ifd[Base.MakerNote.value] = bytes(range(100))

    img.save(out, format="JPEG", quality=80, exif=exif)


def _build_exif_with_gps_jpeg(out: Path) -> None:
    """100x100 JPEG with GPS coords pointing at a deterministic location.

    37° 46' 30" N, 122° 25' 15" W (≈ San Francisco). Tests assert against
    the exact decimal-degrees conversion so this needs to stay stable.
    """
    img = Image.new("RGB", (100, 100), color=(100, 50, 150))

    exif = Image.Exif()
    gps_ifd = exif.get_ifd(Base.GPSInfo.value)
    gps_ifd[GPS.GPSLatitudeRef.value] = "N"
    gps_ifd[GPS.GPSLatitude.value] = (
        IFDRational(37, 1),
        IFDRational(46, 1),
        IFDRational(30, 1),
    )
    gps_ifd[GPS.GPSLongitudeRef.value] = "W"
    gps_ifd[GPS.GPSLongitude.value] = (
        IFDRational(122, 1),
        IFDRational(25, 1),
        IFDRational(15, 1),
    )

    img.save(out, format="JPEG", quality=80, exif=exif)


def _build_exif_none_jpeg(out: Path) -> None:
    """100x100 JPEG explicitly without an EXIF block."""
    img = Image.new("RGB", (100, 100), color=(150, 100, 50))
    img.save(out, format="JPEG", quality=80)


def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    builders: list[tuple[str, Callable[[Path], None]]] = [
        ("tiny.jpg", _build_tiny_jpeg),
        ("tiny.png", _build_tiny_png),
        ("not_an_image.txt", _build_not_an_image),
        ("exif_rich.jpg", _build_exif_rich_jpeg),
        ("exif_with_gps.jpg", _build_exif_with_gps_jpeg),
        ("exif_none.jpg", _build_exif_none_jpeg),
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
