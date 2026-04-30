"""IPTC IIM metadata extraction from JPEG APP13 / Photoshop IRB blocks.

JPEG-only in v0.1; non-JPEG inputs return an empty payload with one warning.
TIFF IPTC (TIFF tag 33723) and PSD parsing are documented out of scope.

Format reference (cited inline in the helpers):

- **JPEG segments** — ``0xFF<marker>`` then a 2-byte big-endian length that
  *includes the length bytes themselves*, then payload. ``SOI`` (``0xFFD8``)
  and ``EOI`` (``0xFFD9``) have no length; ``RST0..RST7`` (``0xFFD0..D7``)
  and ``TEM`` (``0xFF01``) are standalone. After ``SOS`` (``0xFFDA``) comes
  entropy-coded image data, not segments — the walker stops there.
- **APP13** — marker ``0xFFED``. IPTC IIM lives in an APP13 whose payload
  begins with the ASCII signature ``"Photoshop 3.0\\0"``.
- **Photoshop Image Resource Block (IRB)**:

  ::

      "8BIM"  (4 bytes)
      <2-byte BE resource ID>      0x0404 = IPTC IIM
      Pascal name: <1-byte length> <name bytes> <pad to even total>
      <4-byte BE data size>
      <data bytes, padded to even>

- **IIM record**:

  ::

      0x1C                         (tag marker)
      <1-byte record number>
      <1-byte dataset number>
      <2-byte BE size>             high bit set = extended record (out of scope)
      <value bytes>

**Adversarial-input handling.** Every offset read is bounds-checked. A
malformed length that would walk past end-of-buffer causes the iterator to
stop cleanly, never raise. Whatever was parsed before the corruption ships
in :attr:`~pixel_probe.core.extractors.base.ExtractorResult.data`.

**Charset.** IIM record ``(1, 90)`` (CodedCharacterSet) carries an ISO 2022
escape that selects the encoding for text values. The most common (and
recommended) is ``\\x1b%G`` = UTF-8. Unknown escapes fall back to UTF-8 with
a warning surfaced once per file. Per-value decode errors use
``errors="replace"`` so a single corrupt byte never kills the parse.

**Repeatable tags.** IIM allows certain datasets to repeat (Keywords being
the canonical example). Repeatable tags accumulate into ``list[str]``; all
others are scalar strings.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Final

from pixel_probe.exceptions import FileTooLargeError, MissingFileError

from ._signatures import is_known_image_format
from .base import Extractor, ExtractorResult
from .file_info import MAX_FILE_SIZE_BYTES

__all__ = ["IPTC_TAGS", "IptcData", "IptcExtractor"]

#: Type alias for the IPTC payload. Unlike EXIF (whose values can be
#: numbers, tuples, and nested sub-dicts), every IIM dataset we surface is
#: textual: scalar tags become ``str`` and repeatable tags (Keywords)
#: become ``list[str]``. The tighter ``dict[str, str | list[str]]`` keeps
#: that contract visible to type-checkers and downstream consumers.
#: See ADR 0003 — this matches its prediction exactly.
IptcData = dict[str, str | list[str]]

# JPEG markers
_SOI: Final = b"\xff\xd8"
_APP13_MARKER: Final = 0xED
_SOS_MARKER: Final = 0xDA
_EOI_MARKER: Final = 0xD9

#: JPEG markers without a length field. ``RST0..RST7`` are restart markers;
#: ``TEM`` (``0x01``) is a temporary marker. Walking past them is a 2-byte
#: advance.
_STANDALONE_MARKERS: Final[frozenset[int]] = frozenset({0x01, *range(0xD0, 0xD8)})

# Photoshop IRB / IPTC IIM constants
_PHOTOSHOP_SIGNATURE: Final = b"Photoshop 3.0\x00"
_IRB_SIGNATURE: Final = b"8BIM"
_IPTC_RESOURCE_ID: Final = 0x0404
_IIM_TAG_MARKER: Final = 0x1C
_IIM_EXTENDED_FLAG: Final = 0x8000

#: ISO 2022 escape sequence selecting UTF-8 — by far the most common
#: ``CodedCharacterSet`` value in modern files.
_UTF8_ESCAPE: Final = b"\x1b%G"

#: Dataset that carries the charset escape. Consumed for decode selection;
#: deliberately not surfaced in the output dict.
_CHARSET_RECORD: Final = (1, 90)

#: Friendly names for the IIM (record, dataset) pairs we surface. This is a
#: deliberate subset — the IIM spec defines hundreds of datasets, the bulk
#: of which are obsolete or photo-agency-specific.
IPTC_TAGS: Final[dict[tuple[int, int], str]] = {
    (2, 5): "Title",
    (2, 25): "Keywords",
    (2, 55): "DateCreated",
    (2, 80): "Byline",
    (2, 85): "BylineTitle",
    (2, 90): "City",
    (2, 95): "ProvinceState",
    (2, 101): "Country",
    (2, 105): "Headline",
    (2, 116): "Copyright",
    (2, 120): "Caption",
    (2, 122): "CaptionWriter",
}

#: (record, dataset) pairs that may appear multiple times in one file.
#: These accumulate into ``list[str]`` in the result instead of overwriting.
#:
#: Per the IIM spec, several other datasets are also repeatable —
#: ``(2, 27)`` ContentLocationCode, ``(2, 28)`` ContentLocationName,
#: ``(2, 75)`` ObjectAttributeReference. None are in :data:`IPTC_TAGS`
#: today; if you add one, add it here too. The
#: ``test_repeatable_and_scalar_tags_have_disjoint_names`` regression
#: test guards against the silent-overwrite case where a repeatable
#: dataset shares a friendly name with a non-repeatable one.
_REPEATABLE_TAGS: Final[frozenset[tuple[int, int]]] = frozenset({(2, 25)})


def _iter_segments(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Walk JPEG segments after SOI, yielding ``(marker_byte, payload)``.

    Stops cleanly at ``SOS`` (entropy-coded data follows), at ``EOI``, or
    at any malformed length. Never raises — bounds violations are treated
    as end-of-stream so a single bad segment doesn't crash the parse.

    A malformed input that doesn't begin with ``SOI`` yields nothing.
    """
    if not data.startswith(_SOI):
        return
    offset = 2
    n = len(data)
    while offset + 1 < n:
        if data[offset] != 0xFF:
            return
        marker = data[offset + 1]
        # 0xFF padding/fill bytes appear between segments in some encoders.
        if marker == 0xFF:
            offset += 1
            continue
        if marker in (_SOS_MARKER, _EOI_MARKER):
            return
        if marker in _STANDALONE_MARKERS:
            offset += 2
            continue
        if offset + 4 > n:
            return
        length = int.from_bytes(data[offset + 2 : offset + 4], "big")
        # Length includes the 2 length bytes themselves; anything shorter
        # is malformed.
        if length < 2 or offset + 2 + length > n:
            return
        yield marker, data[offset + 4 : offset + 2 + length]
        offset = offset + 2 + length


def _iter_irbs(payload: bytes) -> Iterator[tuple[int, bytes]]:
    """Walk Photoshop Image Resource Blocks, yielding ``(resource_id, data_block)``.

    The caller must have stripped the leading ``"Photoshop 3.0\\0"`` signature
    before calling. Bounds-checks every offset read; malformed structure
    causes a clean stop.
    """
    offset = 0
    n = len(payload)
    # Minimum IRB shape: 4 sig + 2 id + 2 (1-byte name + pad) + 4 size = 12 bytes.
    while offset + 12 <= n:
        if payload[offset : offset + 4] != _IRB_SIGNATURE:
            return
        resource_id = int.from_bytes(payload[offset + 4 : offset + 6], "big")
        name_len = payload[offset + 6]
        # Pascal name: length byte + name bytes, padded so total is even.
        name_total = 1 + name_len
        if name_total % 2:
            name_total += 1
        size_offset = offset + 6 + name_total
        if size_offset + 4 > n:
            return
        data_size = int.from_bytes(payload[size_offset : size_offset + 4], "big")
        data_start = size_offset + 4
        data_end = data_start + data_size
        if data_end > n:
            return
        yield resource_id, payload[data_start:data_end]
        # Data is padded to even length when the data is odd.
        offset = data_end + (data_size % 2)


def _iter_iim_records(block: bytes) -> Iterator[tuple[int, int, bytes]]:
    """Walk IIM records inside an IPTC IRB data block.

    Yields ``(record_number, dataset_number, value_bytes)``. Stops cleanly on
    malformed structure or extended-size records (high bit in size — out of
    scope for v1). The minimum record header is 5 bytes (marker + record +
    dataset + 2-byte size).
    """
    offset = 0
    n = len(block)
    while offset + 5 <= n:
        if block[offset] != _IIM_TAG_MARKER:
            return
        record = block[offset + 1]
        dataset = block[offset + 2]
        size = int.from_bytes(block[offset + 3 : offset + 5], "big")
        if size & _IIM_EXTENDED_FLAG:
            # Extended records use the size field as a length-of-length;
            # we don't support them in v1 and can't safely advance past one.
            return
        end = offset + 5 + size
        if end > n:
            return
        yield record, dataset, block[offset + 5 : end]
        offset = end


def _resolve_charset(value: bytes | None) -> tuple[str, list[str]]:
    """Map an IIM ``CodedCharacterSet`` escape to a Python codec name.

    Returns ``(codec, warnings)``. Unknown or absent escapes default to
    UTF-8; an unrecognised non-empty escape produces one warning so the
    caller can surface it on the result. UTF-8 is the spec-recommended
    encoding and the dominant case in the wild.
    """
    if value is None or value == _UTF8_ESCAPE:
        return "utf-8", []
    return "utf-8", [f"Unrecognized IPTC charset escape {value!r}; falling back to utf-8"]


def _decode(value: bytes, codec: str) -> str:
    """Decode an IIM value with ``codec``; strip a single trailing NUL.

    Uses ``errors="replace"`` so that a stray bad byte in a single tag
    can't kill the rest of the parse. IPTC text in the wild is sometimes
    double-encoded or contains stray bytes; replacement is a more useful
    failure mode than dropping the tag entirely.

    An unrecognised codec name falls back to UTF-8 with replacement
    rather than letting ``LookupError`` propagate.
    :func:`_resolve_charset` only ever returns ``"utf-8"`` today, so this
    branch is unreachable from normal flow — the guard exists so a future
    extension to the charset map can't crash the parser.
    """
    try:
        return value.decode(codec, errors="replace").rstrip("\x00")
    except LookupError:
        return value.decode("utf-8", errors="replace").rstrip("\x00")


class IptcExtractor(Extractor[IptcData]):
    """Read IPTC IIM metadata from a JPEG's APP13 segment.

    No IPTC block → empty payload, no errors (this is normal, not a failure).
    Non-JPEG input → empty payload + one warning. Corrupt IRB or IIM records
    → walk stops cleanly at the corruption boundary; whatever parsed before
    it ships in :attr:`~pixel_probe.core.extractors.base.ExtractorResult.data`.
    """

    name = "iptc"
    # Annotated as ``type[IptcData]`` explicitly — without it, mypy infers
    # ``type[dict]`` for the assignment and clashes with the ABC's
    # ``type[dict[str, Any]]``. With the annotation, the ``Any`` ↔
    # concrete-type variance is accepted (same trick as ExifExtractor).
    payload_type: type[IptcData] = dict

    def extract(self, path: Path) -> ExtractorResult[IptcData]:
        if not path.is_file():
            raise MissingFileError(f"Not a file: {path}")
        size = path.stat().st_size
        if size > MAX_FILE_SIZE_BYTES:
            raise FileTooLargeError(f"{path} is {size:,} bytes; max is {MAX_FILE_SIZE_BYTES:,}")

        # IPTC blocks live in the first ~64 KB of a JPEG (right after SOI);
        # the whole-file load is wasteful for that purpose but bounded by
        # MAX_FILE_SIZE_BYTES (500 MB). Streaming through a windowed reader
        # would be more memory-efficient but adds complexity v0.1 doesn't
        # need — revisit if profiling shows pressure here.
        raw = path.read_bytes()
        data: IptcData = {}

        if not raw.startswith(_SOI):
            # Per ADR 0011: distinguish "image, just not a format we handle"
            # (warning, with empty-dict data so consumers iterating mixed
            # batches don't see false-alarm errors) from "not an image at
            # all" (error, data=None).
            if is_known_image_format(raw):
                return ExtractorResult(
                    self.name,
                    {},
                    warnings=("IPTC unavailable on this format; v0.1 supports JPEG only",),
                )
            return ExtractorResult(
                self.name,
                errors=("File is not a recognized image format; IPTC requires JPEG",),
            )

        irb_payload = self._find_irb_payload(raw)
        if irb_payload is None:
            return ExtractorResult(self.name, data)

        iim_block = self._find_iim_block(irb_payload)
        if iim_block is None:
            return ExtractorResult(self.name, data)

        # Two-pass over IIM records: the charset escape may appear in any
        # position, so we resolve it before decoding any text values.
        records = list(_iter_iim_records(iim_block))
        charset_bytes: bytes | None = None
        for rec, ds, val in records:
            if (rec, ds) == _CHARSET_RECORD:
                charset_bytes = val
                break
        codec, warnings = _resolve_charset(charset_bytes)

        for rec, ds, val in records:
            key = (rec, ds)
            if key == _CHARSET_RECORD:
                continue
            tag_name = IPTC_TAGS.get(key)
            if tag_name is None:
                continue
            decoded = _decode(val, codec)
            if key in _REPEATABLE_TAGS:
                # Either ``None`` (first occurrence) or an existing ``list[str]``
                # by the disjointness invariant in
                # ``test_repeatable_and_scalar_tags_have_disjoint_names`` —
                # a repeatable tag's friendly name never collides with a
                # scalar tag's, so the existing value can't be a ``str``.
                existing = data.get(tag_name)
                if isinstance(existing, list):
                    existing.append(decoded)
                else:
                    data[tag_name] = [decoded]
            else:
                data[tag_name] = decoded

        return ExtractorResult(self.name, data, warnings=tuple(warnings))

    @staticmethod
    def _find_irb_payload(raw: bytes) -> bytes | None:
        """Return the IRB byte sequence inside the first APP13 segment whose
        payload begins with the Photoshop signature, or ``None`` if no such
        segment exists. Multiple APP13 segments are legal; we take the first
        Photoshop one (the IPTC convention)."""
        for marker, payload in _iter_segments(raw):
            if marker == _APP13_MARKER and payload.startswith(_PHOTOSHOP_SIGNATURE):
                return payload[len(_PHOTOSHOP_SIGNATURE) :]
        return None

    @staticmethod
    def _find_iim_block(irb_payload: bytes) -> bytes | None:
        """Return the data block of the first IRB with resource ID
        ``0x0404`` (IPTC IIM), or ``None`` if absent."""
        for resource_id, block in _iter_irbs(irb_payload):
            if resource_id == _IPTC_RESOURCE_ID:
                return block
        return None
