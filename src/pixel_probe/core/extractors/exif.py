"""EXIF metadata extraction via Pillow.

EXIF is structured as IFD (Image File Directory) blocks: a main IFD plus
sub-IFDs reachable by tag pointer. We surface the main IFD's tags as
flat key-value pairs, and break out the GPS sub-IFD into its own ``gps``
sub-dict with convenient ``latitude`` / ``longitude`` decimal-degree
fields alongside the raw DMS rationals.

Per-tag exceptions are caught and recorded as ``errors`` on the result —
one bad tag doesn't kill the rest of the parse. Whole-file failures
(corrupt EXIF block) raise via the orchestrator's catch-all path.

Adversarial-input handling for EXIF specifically: byte-valued tags
larger than ``_MAX_BYTES_INLINE`` are summarized as
``"<binary, N bytes>"`` instead of carried as raw bytes through the
result. ``MakerNote`` (tag 0x927C) is the typical offender — vendor-
specific blobs that can be many KB.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from PIL import Image, UnidentifiedImageError
from PIL.ExifTags import GPSTAGS, TAGS
from PIL.TiffImagePlugin import IFDRational

from pixel_probe.exceptions import MissingFileError

from .base import Extractor, ExtractorResult

__all__ = ["ExifData", "ExifExtractor"]

#: Type alias for the EXIF payload. Schema is genuinely dynamic — EXIF has
#: hundreds of possible tags, almost all optional. ``dict[str, Any]`` is
#: honest, not lazy. (See ADR 0003.)
ExifData = dict[str, Any]

#: Cap on bytes-valued tags before we summarize. MakerNote can be many KB;
#: most legitimate string tags are well under this.
_MAX_BYTES_INLINE: Final = 64

#: Exif sub-IFD pointer in the main IFD. Holds the tags users actually care
#: about (ExposureTime, FNumber, ISOSpeedRatings, FocalLength, ...).
_EXIF_IFD_TAG: Final = 0x8769

#: GPS sub-IFD pointer, defined by the EXIF spec.
_GPS_INFO_TAG: Final = 0x8825

#: Interoperability sub-IFD pointer.
_INTEROP_IFD_TAG: Final = 0xA005

#: Tags whose values are sub-IFD pointers — we walk Exif and GPS explicitly
#: below; Interop is included here so its raw pointer integer doesn't leak
#: into the result, but its sub-IFD is intentionally NOT walked. Interop
#: tags (related-image-file format, thumbnail interop) are rarely useful
#: and walking them would expand the API surface for negligible benefit.
_SUB_IFD_POINTERS: Final = frozenset({_EXIF_IFD_TAG, _GPS_INFO_TAG, _INTEROP_IFD_TAG})


def _normalize(value: Any) -> Any:
    """Convert a Pillow EXIF value into a JSON-friendly Python primitive.

    - ``IFDRational`` → ``float``
    - ``bytes`` longer than the inline cap → ``"<binary, N bytes>"`` summary
    - ``bytes`` decodable as UTF-8 → the decoded string (NUL-trimmed)
    - ``bytes`` not decodable → ``"<binary, N bytes>"`` summary
    - ``tuple`` → recursively-normalized tuple
    - anything else → returned as-is
    """
    if isinstance(value, IFDRational):
        return float(value)
    if isinstance(value, bytes):
        if len(value) > _MAX_BYTES_INLINE:
            return f"<binary, {len(value)} bytes>"
        try:
            return value.decode("utf-8").rstrip("\x00")
        except UnicodeDecodeError:
            return f"<binary, {len(value)} bytes>"
    if isinstance(value, tuple):
        return tuple(_normalize(v) for v in value)
    return value


def _dms_to_decimal(dms: tuple[float, float, float], ref: str) -> float:
    """Convert ``(degrees, minutes, seconds)`` plus an N/S/E/W ref to decimal degrees.

    Pure function — same input always gives the same output, no side effects.
    Hypothesis-tested in ``tests/property/test_exif_normalize.py``.

    Raises ``TypeError`` / ``ValueError`` / ``ZeroDivisionError`` on malformed
    input (non-numeric values, non-3-tuple, etc.) — caller is expected to
    catch and surface as an error string. Input validation gates the call
    site (see :func:`_extract_gps_ifd`); this function trusts its inputs.
    """
    degrees, minutes, seconds = dms
    decimal = degrees + minutes / 60 + seconds / 3600
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


def _extract_gps_ifd(exif: Image.Exif) -> tuple[dict[str, Any], list[str]]:
    """Build a ``gps`` sub-dict from EXIF's GPS sub-IFD.

    **Never raises.** All failures (corrupt sub-IFD pointer, per-tag
    normalization errors, DMS conversion errors) are returned as strings
    in the second tuple element; the caller attaches them to the result
    envelope. Returns ``({}, [])`` when no GPS sub-IFD is present.
    """
    errors: list[str] = []
    try:
        gps_ifd = exif.get_ifd(_GPS_INFO_TAG)
    except Exception as e:  # noqa: BLE001 - we promise to never raise out of this fn
        return {}, [f"GPS sub-IFD: {type(e).__name__}: {e}"]
    if not gps_ifd:
        return {}, errors

    gps: dict[str, Any] = {}
    for tag_id, raw_value in gps_ifd.items():
        try:
            tag_name = GPSTAGS.get(tag_id, f"Tag_{tag_id:#x}")
            gps[tag_name] = _normalize(raw_value)
        except Exception as e:  # noqa: BLE001 - per-tag isolation is the point
            errors.append(f"GPS tag {tag_id}: {type(e).__name__}: {e}")

    # Convenience: surface latitude/longitude as decimal degrees alongside
    # the raw DMS tuples. Callers that want GPS in any practical way want
    # decimals; the raw DMS stays available for those who need it.
    # Accept both tuple and list — Pillow normally returns tuple, but we
    # don't want a defensive shape change to silently drop the conversion.
    lat = gps.get("GPSLatitude")
    lat_ref = gps.get("GPSLatitudeRef")
    if isinstance(lat, tuple | list) and len(lat) == 3 and isinstance(lat_ref, str):
        try:
            gps["latitude"] = _dms_to_decimal(tuple(lat), lat_ref)
        except (TypeError, ValueError, ZeroDivisionError) as e:
            errors.append(f"GPS latitude conversion: {type(e).__name__}: {e}")

    lon = gps.get("GPSLongitude")
    lon_ref = gps.get("GPSLongitudeRef")
    if isinstance(lon, tuple | list) and len(lon) == 3 and isinstance(lon_ref, str):
        try:
            gps["longitude"] = _dms_to_decimal(tuple(lon), lon_ref)
        except (TypeError, ValueError, ZeroDivisionError) as e:
            errors.append(f"GPS longitude conversion: {type(e).__name__}: {e}")

    return gps, errors


class ExifExtractor(Extractor[ExifData]):
    """Read EXIF metadata via Pillow and surface tags as a flat dict.

    No EXIF block → empty payload, no errors (this is normal, not a failure).
    Corrupt EXIF block → empty payload + a single error string describing
    the failure mode (one bad parse doesn't propagate to the orchestrator).
    Per-tag failures → recorded as errors; surviving tags still ship in data.
    """

    name = "exif"
    # Annotated as type[ExifData] explicitly — without it, mypy infers `type[dict]`
    # for the assignment and clashes with the ABC's `type[dict[str, Any]]`. With
    # the annotation, the ``Any`` ↔ concrete-type variance is accepted.
    payload_type: type[ExifData] = dict

    def extract(self, path: Path) -> ExtractorResult[ExifData]:
        if not path.is_file():
            raise MissingFileError(f"Not a file: {path}")

        errors: list[str] = []
        data: ExifData = {}

        try:
            with Image.open(path) as img:
                exif = img.getexif()
        except UnidentifiedImageError:
            return ExtractorResult(
                self.name,
                data,
                warnings=("File is not a recognized image format",),
            )
        except Exception as e:  # noqa: BLE001 - whole-file failure → one error, not a crash
            return ExtractorResult(self.name, data, errors=(f"{type(e).__name__}: {e}",))

        if not exif:
            return ExtractorResult(self.name, data)

        # Main IFD: skip the sub-IFD pointer tags (we walk the sub-IFDs
        # explicitly below). Per-tag exceptions are isolated so one bad tag
        # doesn't take down the rest.
        for tag_id, raw_value in exif.items():
            if tag_id in _SUB_IFD_POINTERS:
                continue
            try:
                tag_name = TAGS.get(tag_id, f"Tag_{tag_id:#x}")
                data[tag_name] = _normalize(raw_value)
            except Exception as e:  # noqa: BLE001 - per-tag isolation
                errors.append(f"Tag {tag_id}: {type(e).__name__}: {e}")

        # Exif sub-IFD: ExposureTime, FNumber, ISOSpeedRatings, FocalLength,
        # DateTimeOriginal, etc. Flatten into the main data dict — tag IDs
        # are unique across IFDs and the consumer doesn't care which IFD a
        # tag lived in.
        try:
            exif_ifd = exif.get_ifd(_EXIF_IFD_TAG)
        except Exception as e:  # noqa: BLE001 - bad sub-IFD → one error, not a crash
            errors.append(f"Exif sub-IFD: {type(e).__name__}: {e}")
            exif_ifd = {}
        for tag_id, raw_value in exif_ifd.items():
            try:
                tag_name = TAGS.get(tag_id, f"Tag_{tag_id:#x}")
                data[tag_name] = _normalize(raw_value)
            except Exception as e:  # noqa: BLE001 - per-tag isolation
                errors.append(f"Exif tag {tag_id}: {type(e).__name__}: {e}")

        # GPS sub-IFD as data["gps"], if present. _extract_gps_ifd promises
        # never to raise — all its failures come back as error strings.
        gps, gps_errors = _extract_gps_ifd(exif)
        errors.extend(gps_errors)
        if gps:
            data["gps"] = gps

        return ExtractorResult(self.name, data, errors=tuple(errors))
