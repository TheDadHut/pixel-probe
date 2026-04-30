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
import io
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

    Authored via Pillow's native :class:`PIL.Image.Exif` API (see module
    docstring for the dep-graph rationale).
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


# IPTC fixture authoring -----------------------------------------------------
#
# Pillow has no high-level writer for IPTC IIM, so we hand-build the APP13 /
# Photoshop IRB / IIM byte sequence and splice it into a freshly-encoded JPEG
# right after SOI. The format produced here mirrors what real-world photo
# software (Photoshop, exiftool, digiKam) writes, and is the same shape that
# ``pixel_probe.core.extractors.iptc`` parses. See that module's docstring
# for the full layout reference.


def _build_iim_record(record: int, dataset: int, value: bytes) -> bytes:
    """Encode a single IIM record. Extended-size values are out of scope."""
    if len(value) >= 0x8000:
        msg = f"IIM value too large for v1 (use of high bit reserved): {len(value)} bytes"
        raise ValueError(msg)
    return bytes([0x1C, record, dataset]) + len(value).to_bytes(2, "big") + value


def _build_app13_with_iim(records: list[tuple[int, int, bytes]]) -> bytes:
    """Build a complete APP13 segment carrying a Photoshop IRB with IPTC IIM."""
    iim_block = b"".join(_build_iim_record(r, d, v) for r, d, v in records)
    # IRB data is padded to even length when its declared size is odd.
    padded = iim_block + (b"\x00" if len(iim_block) % 2 else b"")
    irb = (
        b"8BIM"
        + (0x0404).to_bytes(2, "big")  # resource ID = IPTC IIM
        + b"\x00\x00"  # zero-length Pascal name + padding to even total
        + len(iim_block).to_bytes(4, "big")
        + padded
    )
    payload = b"Photoshop 3.0\x00" + irb
    # APP13 length includes the 2 length bytes themselves.
    length = len(payload) + 2
    return b"\xff\xed" + length.to_bytes(2, "big") + payload


def _splice_after_soi(jpeg_bytes: bytes, segment: bytes) -> bytes:
    """Insert ``segment`` immediately after JPEG SOI. Decoders accept any
    APPn segment in any order before SOS; right-after-SOI is the most
    conventional placement."""
    return jpeg_bytes[:2] + segment + jpeg_bytes[2:]


def _build_iptc_basic_jpeg(out: Path) -> None:
    """100x100 JPEG with rich IPTC: title, multi-keyword (3), byline, copyright,
    and an explicit UTF-8 charset escape so the parser exercises the
    ``CodedCharacterSet`` resolution path."""
    img = Image.new("RGB", (100, 100), color=(50, 100, 150))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    base = buf.getvalue()

    records: list[tuple[int, int, bytes]] = [
        (1, 90, b"\x1b%G"),  # CodedCharacterSet → UTF-8
        (2, 5, b"Sample IPTC Title"),
        (2, 25, b"alpha"),  # Keywords (repeatable)
        (2, 25, b"beta"),
        (2, 25, b"gamma"),
        (2, 80, b"Test Author"),
        (2, 116, "© 2026 TestCorp".encode()),  # explicitly UTF-8 to exercise non-ASCII
    ]
    out.write_bytes(_splice_after_soi(base, _build_app13_with_iim(records)))


def _build_iptc_none_jpeg(out: Path) -> None:
    """100x100 JPEG explicitly without any IPTC block."""
    img = Image.new("RGB", (100, 100), color=(150, 100, 50))
    img.save(out, format="JPEG", quality=80)


def _build_iptc_corrupt_jpeg(out: Path) -> None:
    """100x100 JPEG whose IIM record declares a value-size 9999 bytes long
    when only ~9 follow. The IRB itself is well-formed; only the IIM walker
    encounters the corruption. The parser must detect the bounds violation
    and stop cleanly without raising — exercising the ``end > n`` guard
    in :func:`_iter_iim_records`."""
    img = Image.new("RGB", (100, 100), color=(120, 120, 120))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    base = buf.getvalue()

    # 0x1C 02 05 (Title) + declared size 9999 + only 9 bytes of value.
    # The IRB declares its data size accurately (the parser advances past
    # this whole block correctly); only the inner IIM walk hits the lie.
    iim_corrupt = bytes([0x1C, 0x02, 0x05]) + (9999).to_bytes(2, "big") + b"truncated"
    padded = iim_corrupt + (b"\x00" if len(iim_corrupt) % 2 else b"")
    irb = (
        b"8BIM"
        + (0x0404).to_bytes(2, "big")
        + b"\x00\x00"
        + len(iim_corrupt).to_bytes(4, "big")
        + padded
    )
    payload = b"Photoshop 3.0\x00" + irb
    length = len(payload) + 2
    app13 = b"\xff\xed" + length.to_bytes(2, "big") + payload
    out.write_bytes(_splice_after_soi(base, app13))


def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    builders: list[tuple[str, Callable[[Path], None]]] = [
        ("tiny.jpg", _build_tiny_jpeg),
        ("tiny.png", _build_tiny_png),
        ("not_an_image.txt", _build_not_an_image),
        ("exif_rich.jpg", _build_exif_rich_jpeg),
        ("exif_with_gps.jpg", _build_exif_with_gps_jpeg),
        ("exif_none.jpg", _build_exif_none_jpeg),
        ("iptc_basic.jpg", _build_iptc_basic_jpeg),
        ("iptc_none.jpg", _build_iptc_none_jpeg),
        ("iptc_corrupt.jpg", _build_iptc_corrupt_jpeg),
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
