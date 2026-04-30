"""EXIF extractor tests — tag parsing, GPS conversion, error isolation, MakerNote truncation."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from PIL.Image import Exif
from PIL.TiffImagePlugin import IFDRational

from pixel_probe.core.extractors import exif as exif_module
from pixel_probe.core.extractors.exif import ExifExtractor, _dms_to_decimal, _normalize
from pixel_probe.exceptions import MissingFileError

from .conftest import fixture_path


def test_returns_empty_for_image_without_exif() -> None:
    """No EXIF block is normal — return empty data, no errors, no warnings."""
    result = ExifExtractor().extract(fixture_path("exif_none.jpg"))

    assert result.extractor_name == "exif"
    assert result.data == {}
    assert result.errors == ()
    assert result.warnings == ()
    assert not result.has_data


def test_parses_basic_tags() -> None:
    """Rich EXIF fixture exercises Make/Model from the main IFD plus
    ExposureTime/FNumber/ISO/FocalLength/DateTimeOriginal from the Exif
    sub-IFD — both should be surfaced in one flat dict."""
    result = ExifExtractor().extract(fixture_path("exif_rich.jpg"))

    assert result.errors == ()
    assert result.has_data

    data = result.data
    # Main IFD
    assert data["Make"] == "TestCorp"
    assert data["Model"] == "TestModel X1"
    assert data["DateTime"] == "2026:01:15 14:30:00"
    # Exif sub-IFD (proves we follow the ExifOffset pointer)
    assert data["DateTimeOriginal"] == "2026:01:15 14:30:00"
    assert data["ISOSpeedRatings"] == 400
    assert data["FocalLength"] == 50.0
    assert data["ExposureTime"] == pytest.approx(1 / 250)
    assert data["FNumber"] == pytest.approx(2.8)


def test_does_not_surface_sub_ifd_pointers() -> None:
    """ExifOffset / GPSInfo / InteroperabilityIFDPointer are pointers into
    sub-IFDs — we walk the sub-IFD and surface its tags. The raw pointer
    integer must not appear in the result."""
    result = ExifExtractor().extract(fixture_path("exif_rich.jpg"))

    assert "ExifOffset" not in result.data
    assert "GPSInfo" not in result.data
    assert "InteroperabilityIFD" not in result.data


def test_gps_decimal_conversion() -> None:
    """GPS DMS coordinates → decimal degrees with correct sign for N/S/E/W.

    Fixture is at 37° 46' 30" N, 122° 25' 15" W. Verify both the raw DMS
    triple and the convenience decimal-degree fields."""
    result = ExifExtractor().extract(fixture_path("exif_with_gps.jpg"))

    assert result.errors == ()
    gps = result.data["gps"]

    # Raw DMS preserved
    assert gps["GPSLatitude"] == (37.0, 46.0, 30.0)
    assert gps["GPSLatitudeRef"] == "N"
    assert gps["GPSLongitude"] == (122.0, 25.0, 15.0)
    assert gps["GPSLongitudeRef"] == "W"

    # Decimal-degrees convenience fields with correct hemisphere signs
    assert gps["latitude"] == pytest.approx(37 + 46 / 60 + 30 / 3600)
    # West → negative
    assert gps["longitude"] == pytest.approx(-(122 + 25 / 60 + 15 / 3600))


def test_handles_corrupt_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``Image.getexif`` raises (corrupt EXIF block in the wild), we
    must surface a single error string and not propagate the exception
    out — the orchestrator's catch-all isn't the right boundary for this
    because ``getexif`` errors are localized to one extractor."""
    real_open = Image.open

    def _broken_getexif(self: Any) -> Any:
        msg = "fake corruption from monkeypatch"
        raise ValueError(msg)

    def _open_with_broken_exif(*args: object, **kwargs: object) -> Any:
        img = real_open(*args, **kwargs)
        # The patched method must accept self because Image.Exif uses bound dispatch.
        monkeypatch.setattr(img.__class__, "getexif", _broken_getexif, raising=True)
        return img

    monkeypatch.setattr(Image, "open", _open_with_broken_exif)

    result = ExifExtractor().extract(fixture_path("exif_rich.jpg"))

    # Per ADR 0011, zero-data failure → data=None.
    assert result.data is None
    assert len(result.errors) == 1
    assert "ValueError" in result.errors[0]
    assert "fake corruption" in result.errors[0]


def test_extractor_truncates_oversized_makernote() -> None:
    """End-to-end: the rich fixture deliberately includes a 100-byte
    MakerNote that exceeds ``_MAX_BYTES_INLINE`` (64). The extractor must
    surface it as a ``<binary, N bytes>`` summary, not the raw bytes —
    that's the adversarial-input gate against vendor-specific blobs."""
    result = ExifExtractor().extract(fixture_path("exif_rich.jpg"))

    assert "MakerNote" in result.data
    summary = result.data["MakerNote"]
    assert isinstance(summary, str)
    assert summary.startswith("<binary,")
    assert summary.endswith(" bytes>")
    # Pillow may add a trailing NUL or pad; assert >= rather than == so the
    # test isn't fragile to encoder quirks. Anything well over the cap is fine.
    match = summary.replace("<binary, ", "").replace(" bytes>", "")
    assert int(match) >= 100


def test_normalize_decodes_short_utf8_bytes() -> None:
    """Short UTF-8-decodable bytes become strings; trailing NUL is stripped."""
    assert _normalize(b"hello") == "hello"
    assert _normalize(b"hello\x00") == "hello"


def test_normalize_summarizes_undecodable_short_bytes() -> None:
    """Short but non-UTF-8 bytes get summarized — we don't carry raw bytes
    through the result envelope."""
    assert _normalize(b"\xff\xfe\xfd") == "<binary, 3 bytes>"


def test_normalize_converts_ifdrational() -> None:
    """``IFDRational`` (Pillow's rational-number type) becomes a float —
    JSON-friendly and the only sane way to surface 1/250 to a user."""
    rational = IFDRational(1, 250)
    assert _normalize(rational) == pytest.approx(1 / 250)


def test_normalize_recurses_into_tuples() -> None:
    """Tuples of mixed types (the GPS DMS shape) are normalised element-wise."""
    rational_tuple = (IFDRational(37, 1), IFDRational(46, 1), IFDRational(30, 1))
    assert _normalize(rational_tuple) == (37.0, 46.0, 30.0)


def test_dms_to_decimal_handles_all_four_hemispheres() -> None:
    """N/E are positive; S/W flip the sign."""
    assert _dms_to_decimal((37.0, 46.0, 30.0), "N") == pytest.approx(37.775)
    assert _dms_to_decimal((37.0, 46.0, 30.0), "S") == pytest.approx(-37.775)
    assert _dms_to_decimal((122.0, 25.0, 15.0), "E") == pytest.approx(122.42083333)
    assert _dms_to_decimal((122.0, 25.0, 15.0), "W") == pytest.approx(-122.42083333)


def test_missing_file_raises_typed_error(tmp_path: Path) -> None:
    """Same boundary contract as :class:`FileInfoExtractor`: missing input
    is a :class:`MissingFileError` (a :class:`PixelProbeError` subclass)."""
    with pytest.raises(MissingFileError):
        ExifExtractor().extract(tmp_path / "does-not-exist.jpg")


def test_unrecognised_format_returns_error_not_crash() -> None:
    """A non-image file returns an error per ADR 0011 — extractor produced
    zero data, so failure semantics apply (data=None, error not warning).
    Diverges from FileInfoExtractor's contract because file_info ships
    partial data (file-level fields) for non-images, while EXIF doesn't."""
    result = ExifExtractor().extract(fixture_path("not_an_image.txt"))

    assert result.data is None
    assert result.warnings == ()
    assert result.errors == ("File is not a recognized image format",)


def test_idempotent() -> None:
    """Same input → same output, every call."""
    extractor = ExifExtractor()
    a = extractor.extract(fixture_path("exif_rich.jpg"))
    b = extractor.extract(fixture_path("exif_rich.jpg"))
    assert a.data == b.data
    assert a.errors == b.errors


def test_only_finite_floats_for_valid_gps() -> None:
    """Defensive: the convenience decimal-degree fields must always be
    finite floats for valid input. NaN / inf would be a regression."""
    result = ExifExtractor().extract(fixture_path("exif_with_gps.jpg"))
    gps = result.data["gps"]
    assert math.isfinite(gps["latitude"])
    assert math.isfinite(gps["longitude"])
    assert -90 <= gps["latitude"] <= 90
    assert -180 <= gps["longitude"] <= 180


def test_per_tag_exception_isolated_to_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``_normalize`` raises on one tag, the bad tag becomes an error
    string but the surviving tags still ship in ``data``. This is the
    per-tag-isolation contract called out in the file's module docstring."""
    real_normalize = exif_module._normalize
    boom_value = "TestCorp"  # Make tag value (Pillow surfaces it as str)

    def _selectively_broken_normalize(value: object) -> object:
        if value == boom_value:
            msg = "synthetic per-tag failure"
            raise RuntimeError(msg)
        return real_normalize(value)

    monkeypatch.setattr(exif_module, "_normalize", _selectively_broken_normalize)

    result = ExifExtractor().extract(fixture_path("exif_rich.jpg"))

    # The Make tag failed; the rest of the data still extracted.
    assert "Make" not in result.data
    assert "Model" in result.data
    assert any("RuntimeError" in e and "synthetic per-tag failure" in e for e in result.errors)


def test_corrupt_exif_sub_ifd_recorded_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``exif.get_ifd`` raises while resolving the Exif sub-IFD, surface
    a single error and continue — main IFD tags + GPS still get parsed."""
    original_get_ifd = Exif.get_ifd

    def _broken(self: Exif, tag: int) -> Any:
        if tag == 0x8769:  # Exif sub-IFD pointer
            msg = "synthetic sub-IFD failure"
            raise RuntimeError(msg)
        return original_get_ifd(self, tag)

    monkeypatch.setattr(Exif, "get_ifd", _broken)

    result = ExifExtractor().extract(fixture_path("exif_with_gps.jpg"))

    # GPS still parsed (the broken get_ifd specifically targeted ExifIFD).
    assert "gps" in result.data
    assert any("Exif sub-IFD" in e and "RuntimeError" in e for e in result.errors)


def test_corrupt_gps_sub_ifd_recorded_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``exif.get_ifd`` raises while resolving the GPS sub-IFD, the
    helper catches it (R1: clean error contract — ``_extract_gps_ifd``
    never raises) and surfaces a single error string. Main-IFD tags
    survive; the result still ships the rest of the data."""
    original_get_ifd = Exif.get_ifd

    def _broken(self: Exif, tag: int) -> Any:
        if tag == 0x8825:  # GPS sub-IFD pointer
            msg = "synthetic GPS failure"
            raise RuntimeError(msg)
        return original_get_ifd(self, tag)

    monkeypatch.setattr(Exif, "get_ifd", _broken)

    result = ExifExtractor().extract(fixture_path("exif_with_gps.jpg"))

    assert any("GPS sub-IFD" in e and "RuntimeError" in e for e in result.errors)
    # No "gps" sub-dict because we couldn't reach the GPS data at all.
    assert "gps" not in result.data


def test_exif_sub_ifd_per_tag_exception_isolated_to_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-tag isolation parity: same contract as the main-IFD test, but
    targeting a tag that lives in the Exif sub-IFD. The 100-byte MakerNote
    value in ``exif_rich.jpg`` is unique to that sub-IFD walk, so the
    raised error gets formatted with the ``Exif tag {tag_id}: ...`` prefix
    rather than the main-IFD ``Tag {tag_id}: ...`` prefix."""
    real_normalize = exif_module._normalize

    def _selective_failure(value: object) -> object:
        # The 100-byte MakerNote in exif_rich.jpg is the only bytes value
        # at this exact length — Exif sub-IFD only.
        if isinstance(value, bytes) and len(value) == 100:
            msg = "synthetic Exif sub-IFD per-tag failure"
            raise RuntimeError(msg)
        return real_normalize(value)

    monkeypatch.setattr(exif_module, "_normalize", _selective_failure)

    result = ExifExtractor().extract(fixture_path("exif_rich.jpg"))

    assert "MakerNote" not in result.data
    assert "Make" in result.data  # main-IFD tags untouched
    assert any(
        "Exif tag" in e and "RuntimeError" in e and "synthetic Exif sub-IFD" in e
        for e in result.errors
    )


def test_extract_gps_ifd_handles_partial_data() -> None:
    """If a GPS sub-IFD has latitude data but no longitude (or vice versa),
    the available conversion still runs; the missing one is silently
    skipped via the isinstance gate. Direct unit test of
    :func:`_extract_gps_ifd` against a stubbed Exif object — covers the
    branch where the lon-side ``isinstance`` check fails but lat-side
    succeeded."""

    class _StubExif:
        """Duck-typed stand-in for ``PIL.Image.Exif`` — the helper only
        needs ``.get_ifd(tag)``; we don't need a real Pillow object."""

        def get_ifd(self, tag: int) -> dict[int, Any]:
            del tag
            # Use already-flat floats — _normalize would normally produce these
            # from IFDRationals, but our stub bypasses Pillow's parsing.
            return {
                0x0001: "N",  # GPSLatitudeRef
                0x0002: (37.0, 46.0, 30.0),  # GPSLatitude
                # GPSLongitude / GPSLongitudeRef deliberately absent.
            }

    gps, errors = exif_module._extract_gps_ifd(_StubExif())  # type: ignore[arg-type]

    assert errors == []
    assert "latitude" in gps  # lat conversion ran
    assert "longitude" not in gps  # lon block skipped via isinstance gate


def test_gps_dms_conversion_errors_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``_dms_to_decimal`` raises, both the latitude and longitude
    conversion attempts append error strings — but the raw DMS rationals
    still ship in ``data["gps"]``. This covers the defensive
    ``(TypeError, ValueError, ZeroDivisionError)`` catches: they exist
    for unusual GPS encodings that pass the isinstance gate but would
    crash arithmetic (e.g. a tuple of strings that monkey-patched
    ``_normalize`` returned unchanged)."""

    def _broken(dms: tuple[float, float, float], ref: str) -> float:
        del dms, ref
        msg = "synthetic DMS conversion failure"
        raise ValueError(msg)

    monkeypatch.setattr(exif_module, "_dms_to_decimal", _broken)

    result = ExifExtractor().extract(fixture_path("exif_with_gps.jpg"))

    lat_errors = [e for e in result.errors if "GPS latitude conversion" in e]
    lon_errors = [e for e in result.errors if "GPS longitude conversion" in e]
    assert len(lat_errors) == 1
    assert len(lon_errors) == 1
    assert "ValueError" in lat_errors[0]

    # Raw DMS data still preserved — only the decimal-conversion step failed.
    gps = result.data["gps"]
    assert gps["GPSLatitude"] == (37.0, 46.0, 30.0)
    assert gps["GPSLongitude"] == (122.0, 25.0, 15.0)
    # Decimal-degree convenience fields not produced (conversion failed).
    assert "latitude" not in gps
    assert "longitude" not in gps


def test_gps_per_tag_exception_isolated_to_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``_normalize`` raises on one GPS tag, the bad tag becomes an
    error string but the rest of the GPS sub-IFD still parses. Parity
    with the main-IFD per-tag-isolation contract — verifies the same
    discipline applies inside ``_extract_gps_ifd``'s loop."""
    real_normalize = exif_module._normalize

    def _selective_failure(value: object) -> object:
        # GPSLatitudeRef in the fixture is "N" (str). Fail on it specifically.
        if value == "N":
            msg = "synthetic GPS per-tag failure"
            raise RuntimeError(msg)
        return real_normalize(value)

    monkeypatch.setattr(exif_module, "_normalize", _selective_failure)

    result = ExifExtractor().extract(fixture_path("exif_with_gps.jpg"))

    # The bad tag became an error.
    assert any(
        "GPS tag" in e and "RuntimeError" in e and "synthetic GPS per-tag failure" in e
        for e in result.errors
    )
    # Sibling GPS tags still made it through.
    gps = result.data["gps"]
    assert "GPSLatitude" in gps
    assert "GPSLongitudeRef" in gps
