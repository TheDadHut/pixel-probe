"""XMP metadata extraction via :mod:`defusedxml`.

XMP (Extensible Metadata Platform) is XML data embedded in image files.
v0.1 supports two host formats:

- **JPEG** — APP1 segment (``0xFFE1``) whose payload begins with the ASCII
  signature ``"http://ns.adobe.com/xap/1.0/\\0"``. Extended XMP via
  ``xmpNote:HasExtendedXMP`` is documented out of scope; we warn if
  detected and parse only the main packet.
- **PNG** — iTXt chunk with keyword ``XML:com.adobe.xmp``. Both
  uncompressed (compression-flag = 0) and zlib-compressed
  (compression-flag = 1) chunks are supported via stdlib :mod:`zlib`.
  Decompression is bounded by :data:`_MAX_DECOMPRESSED_XMP_BYTES` to
  defend against decompression-bomb DoS. Malformed compressed streams
  and oversized inflated payloads surface as errors on the result
  envelope (matching the malformed-XML severity) rather than raising
  out of the extractor.

TIFF (tag 700) is out of scope for v1 — TIFF support overall is deferred
until a real-world need surfaces.

**Security**. Parsing goes through :func:`defusedxml.ElementTree.fromstring`,
which refuses DTDs and external entities by default. An XMP packet
carrying an XXE payload triggers a :class:`defusedxml.DefusedXmlException`
(via :class:`~defusedxml.EntitiesForbidden` / :class:`~defusedxml.DTDForbidden`)
and surfaces as an error string on the result envelope — no file
contents are ever resolved or leaked. This is deliberate portfolio
signal: see ADR 0003 and the security note in PLAN.md §6.

**Output shape**. Properties are flattened into a nested dict keyed by
friendly namespace prefix (``dc``, ``xmp``, ``photoshop``, ``exif``,
``tiff``, ``Iptc4xmpCore``):

.. code-block:: python

    {
        "dc": {"title": "Photo title", "subject": ["nature", "landscape"]},
        "xmp": {"CreatorTool": "Adobe Lightroom 12.0"},
        ...
    }

RDF flattening rules:

- ``<rdf:Bag>`` / ``<rdf:Seq>`` of ``<rdf:li>`` → ``list[str]``
- ``<rdf:Alt>`` of language-tagged ``<rdf:li>`` → pick ``xml:lang="x-default"``,
  fall back to first ``<rdf:li>``
- Attributes on ``<rdf:Description>`` → simple string fields directly
- Multiple ``<rdf:Description>`` blocks → fields merged
- Properties from namespaces not in :data:`_NAMESPACE_PREFIXES` are
  dropped (the XMP namespace zoo has hundreds of vendor-specific schemas;
  surfacing them all as ``nsN_field`` would be noise) — but a single
  batched warning listing the dropped namespace URIs surfaces on the
  result envelope so the omission is visible to consumers.
"""

from __future__ import annotations

# ParseError comes from the stdlib but is what defusedxml raises for malformed
# XML. Importing the stdlib symbol keeps the typing edge sharp; defusedxml
# only adds DTD/entity rejection on top of the stdlib parser.
import zlib
from pathlib import Path
from typing import Any, Final
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from pixel_probe.exceptions import FileTooLargeError, MissingFileError

from .base import Extractor, ExtractorResult
from .file_info import MAX_FILE_SIZE_BYTES

__all__ = ["XmpData", "XmpExtractor"]

#: Type alias for the XMP payload. Top-level keys are friendly namespace
#: prefixes (``dc``, ``xmp``, ``photoshop`` …); their values are field maps.
#: Inner-dict values are ``str``, ``list[str]``, or ``dict[str, Any]`` —
#: the last for structured properties (``exif:Flash``, etc.) that flatten
#: to a sub-record. The ``Any`` at the inner level absorbs that union
#: honestly. Matches ADR 0003's prediction exactly.
XmpData = dict[str, dict[str, Any]]

# JPEG / PNG host-format constants
_JPEG_SOI: Final = b"\xff\xd8"
_JPEG_APP1_MARKER: Final = 0xE1
_JPEG_SOS_MARKER: Final = 0xDA
_JPEG_EOI_MARKER: Final = 0xD9
_JPEG_STANDALONE_MARKERS: Final[frozenset[int]] = frozenset({0x01, *range(0xD0, 0xD8)})
_XMP_JPEG_SIGNATURE: Final = b"http://ns.adobe.com/xap/1.0/\x00"

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_PNG_ITXT_TYPE: Final = b"iTXt"
_PNG_XMP_KEYWORD: Final = b"XML:com.adobe.xmp"

#: Output cap on zlib-decompressed iTXt payloads. Defends against
#: decompression-bomb DoS (a small compressed chunk that inflates to
#: gigabytes), parallel to ADR 0007's Pillow ``MAX_IMAGE_PIXELS`` gate.
#: Real-world XMP packets are well under 100 KB; 16 MB is generous
#: enough to never reject a legitimate file while bounding adversarial
#: input by ~32:1 against the project-wide 500 MB file cap.
_MAX_DECOMPRESSED_XMP_BYTES: Final = 16 * 1024 * 1024

# RDF / XML namespace URIs — used for ElementTree's '{uri}local' form.
_RDF_NS: Final = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_XML_NS: Final = "http://www.w3.org/XML/1998/namespace"
_XMP_NOTE_NS: Final = "http://ns.adobe.com/xmp/note/"

_RDF_RDF: Final = f"{{{_RDF_NS}}}RDF"
_RDF_DESCRIPTION: Final = f"{{{_RDF_NS}}}Description"
_RDF_BAG: Final = f"{{{_RDF_NS}}}Bag"
_RDF_SEQ: Final = f"{{{_RDF_NS}}}Seq"
_RDF_ALT: Final = f"{{{_RDF_NS}}}Alt"
_RDF_LI: Final = f"{{{_RDF_NS}}}li"
_XML_LANG: Final = f"{{{_XML_NS}}}lang"
_HAS_EXTENDED_XMP: Final = f"{{{_XMP_NOTE_NS}}}HasExtendedXMP"

#: Namespace URI → friendly prefix. Properties from URIs not in this map are
#: dropped, but ``_flatten_xmp`` surfaces a single batched warning listing
#: which namespaces were dropped — XMP has a long tail of vendor-specific
#: schemas and surfacing all of them as ``ns0_field`` etc. would be noise,
#: but silently swallowing them was a "looks like nothing matched" footgun.
_NAMESPACE_PREFIXES: Final[dict[str, str]] = {
    "http://purl.org/dc/elements/1.1/": "dc",
    "http://ns.adobe.com/xap/1.0/": "xmp",
    "http://ns.adobe.com/photoshop/1.0/": "photoshop",
    "http://ns.adobe.com/exif/1.0/": "exif",
    "http://ns.adobe.com/tiff/1.0/": "tiff",
    "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/": "Iptc4xmpCore",
}

#: URIs that carry RDF / XML bookkeeping rather than user metadata. Excluded
#: from the dropped-namespace warning so the warning lists only namespaces
#: that *would* have been surfaced if the friendly map covered them — i.e.
#: actual vendor / extension schemas the user might be missing.
#:
#: - ``_RDF_NS`` — ``rdf:about``, ``rdf:type``, etc. on Description.
#: - ``_XML_NS`` — ``xml:lang`` and friends.
#: - ``_XMP_NOTE_NS`` — only ``xmpNote:HasExtendedXMP`` per spec; detected
#:   by :func:`_has_extended_xmp` and excluded here so the marker doesn't
#:   produce a noise warning. If Adobe ever extends this namespace beyond
#:   the marker, the new properties would be silently dropped — accept this
#:   risk because the spec is stable.
#: - ``adobe:ns:meta/`` — defensive: the ``x:xmpmeta`` wrapper's own
#:   attributes aren't currently iterated by the walker, but the entry
#:   keeps us safe if a future code path ever does.
_BOOKKEEPING_NAMESPACES: Final[frozenset[str]] = frozenset(
    {
        _RDF_NS,
        _XML_NS,
        _XMP_NOTE_NS,
        "adobe:ns:meta/",
    }
)


def _find_xmp_packet(raw: bytes) -> tuple[bytes | None, list[str], list[str]]:
    """Locate the XMP packet bytes in a JPEG or PNG byte stream.

    Dispatches on file signature. Returns ``(packet_or_None, warnings,
    errors)`` — warnings and errors both carry diagnostics from the
    host-format walker so the extractor can surface them on the result
    envelope at the right severity. Errors describe failed-parse cases
    ("we found something but couldn't decode it"); warnings describe
    non-fatal anomalies. ``packet`` is ``None`` when the host format is
    unsupported, no XMP packet is present, or every found chunk failed
    to decode.
    """
    if raw.startswith(_JPEG_SOI):
        return _find_xmp_packet_jpeg(raw), [], []
    if raw.startswith(_PNG_SIGNATURE):
        return _find_xmp_packet_png(raw)
    return None, [], []


def _find_xmp_packet_jpeg(raw: bytes) -> bytes | None:
    """Walk JPEG segments looking for the first APP1 with the XMP signature.

    Bounds-checks every length read; malformed structure terminates the
    walk cleanly without raising. Stops at SOS or EOI — entropy-coded
    image data follows SOS and isn't a segment.
    """
    offset = 2  # skip SOI
    n = len(raw)
    while offset + 1 < n:
        if raw[offset] != 0xFF:
            return None
        marker = raw[offset + 1]
        if marker == 0xFF:  # padding fill byte
            offset += 1
            continue
        if marker in (_JPEG_SOS_MARKER, _JPEG_EOI_MARKER):
            return None
        if marker in _JPEG_STANDALONE_MARKERS:
            offset += 2
            continue
        if offset + 4 > n:
            return None
        length = int.from_bytes(raw[offset + 2 : offset + 4], "big")
        if length < 2 or offset + 2 + length > n:
            return None
        payload = raw[offset + 4 : offset + 2 + length]
        if marker == _JPEG_APP1_MARKER and payload.startswith(_XMP_JPEG_SIGNATURE):
            return payload[len(_XMP_JPEG_SIGNATURE) :]
        offset = offset + 2 + length
    return None


def _find_xmp_packet_png(raw: bytes) -> tuple[bytes | None, list[str], list[str]]:
    """Walk PNG chunks looking for an iTXt chunk with the XMP keyword.

    PNG chunk layout: ``<4-byte BE length> <4-byte type> <data> <4-byte CRC>``.
    The length field describes ``data`` only — the type field and CRC are
    fixed-size. We bounds-check before each chunk read.

    Returns ``(packet_or_None, warnings, errors)``. Errors collect
    "we found an XMP iTXt chunk but couldn't decode it" cases so they
    surface at the right severity; warnings stay reserved for non-fatal
    anomalies (currently none from this walker, but the slot is plumbed
    for parity with the dispatcher's tuple shape).
    """
    offset = len(_PNG_SIGNATURE)
    n = len(raw)
    warnings: list[str] = []
    errors: list[str] = []
    # Minimum chunk shape: 4 length + 4 type + 4 CRC = 12 bytes.
    while offset + 12 <= n:
        length = int.from_bytes(raw[offset : offset + 4], "big")
        chunk_type = raw[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        # data_end + 4 because the CRC follows the data.
        if data_end + 4 > n:
            return None, warnings, errors
        if chunk_type == _PNG_ITXT_TYPE:
            packet, chunk_warnings, chunk_errors = _read_itxt_xmp(raw[data_start:data_end])
            warnings.extend(chunk_warnings)
            errors.extend(chunk_errors)
            if packet is not None:
                return packet, warnings, errors
        offset = data_end + 4  # next chunk: skip CRC
    return None, warnings, errors


def _read_itxt_xmp(data: bytes) -> tuple[bytes | None, list[str], list[str]]:
    """Parse an iTXt chunk payload; return its text only when keyword is XMP.

    iTXt structure: ``keyword \\0 compression-flag(1) compression-method(1)
    language-tag \\0 translated-keyword \\0 text``. We bounds-check each
    NUL-terminated section and bail on truncation.

    Returns ``(packet_or_None, warnings, errors)``. PNG spec defines
    compression flag 0 (uncompressed) and 1 (zlib); we decompress flag 1
    via :mod:`zlib` (stdlib — no extra runtime dep). Decompression is
    bounded by :data:`_MAX_DECOMPRESSED_XMP_BYTES` to prevent
    decompression-bomb DoS — a small compressed chunk that would inflate
    to gigabytes is rejected without allocating its full output.

    Failure cases are surfaced as **errors** (not warnings): malformed
    compressed streams (``zlib.error``), oversized decompression
    (suspected bomb), and non-spec compression flags. Each is "we found
    an XMP iTXt chunk and couldn't decode it" — the same severity as the
    extractor's malformed-XML path.
    """
    nul = data.find(b"\x00")
    if nul == -1 or data[:nul] != _PNG_XMP_KEYWORD:
        return None, [], []
    # Need at least: nul + compression-flag + compression-method = nul + 3
    if len(data) < nul + 3:
        return None, [], []
    compression_flag = data[nul + 1]
    # Skip language tag (\0-terminated) and translated keyword (\0-terminated).
    lang_start = nul + 3
    lang_end = data.find(b"\x00", lang_start)
    if lang_end == -1:
        return None, [], []
    tkw_end = data.find(b"\x00", lang_end + 1)
    if tkw_end == -1:
        return None, [], []
    text = data[tkw_end + 1 :]
    if compression_flag == 0:
        return text, [], []
    if compression_flag == 1:
        try:
            decompressor = zlib.decompressobj()
            decoded = decompressor.decompress(text, _MAX_DECOMPRESSED_XMP_BYTES)
            # If decompressor.unconsumed_tail is non-empty, more data was
            # available than the cap allowed — i.e. the inflated payload
            # exceeds our threshold. Don't call flush(); doing so would
            # decompress the rest and defeat the cap. Reject as a bomb.
            if decompressor.unconsumed_tail:
                return (
                    None,
                    [],
                    [
                        f"Compressed XMP iTXt chunk exceeds the "
                        f"{_MAX_DECOMPRESSED_XMP_BYTES:,}-byte decompression cap; "
                        f"rejected as suspected decompression bomb"
                    ],
                )
            decoded += decompressor.flush()
        except zlib.error as e:
            return None, [], [f"Malformed compressed XMP iTXt chunk: {type(e).__name__}: {e}"]
        return decoded, [], []
    # Non-spec flag values (PNG defines 0 and 1 only).
    return None, [], [f"Unrecognized iTXt compression flag {compression_flag}; chunk ignored"]


def _strip_namespace(qname: str) -> tuple[str | None, str]:
    """Split ElementTree's ``{uri}local`` form into ``(uri, local)``.

    Elements / attributes without a namespace return ``(None, qname)``.
    """
    if qname.startswith("{"):
        end = qname.find("}")
        if end > 0:
            return qname[1:end], qname[end + 1 :]
    return None, qname


def _flatten_value(elem: Element) -> str | list[str] | dict[str, Any]:
    """Convert an XMP property element to a Python primitive.

    - ``<rdf:Bag>`` / ``<rdf:Seq>`` → ``list[str]`` (in document order)
    - ``<rdf:Alt>`` → the ``x-default`` lang's text, or first ``rdf:li``
      if no ``x-default`` is present
    - **Nested ``<rdf:Description>``** → flattened to a ``dict[str, Any]``
      of its attribute-and-element fields (e.g., ``exif:Flash`` →
      ``{"Fired": "True", "Mode": "0", ...}``). Sub-fields from namespaces
      outside :data:`_NAMESPACE_PREFIXES` are dropped, same policy as the
      top-level walker.
    - anything else → the element's stripped text content (empty string
      when absent)

    A nested ``rdf:Description`` *inside* a Bag/Seq/Alt container (a
    structured value within a list/alt) is not unwrapped — the list-style
    flattening returns ``li.text`` only, so structured items become empty
    strings. Real-world XMP rarely uses this combination; documenting as
    a v0.1 limitation rather than recursing further.
    """
    children = list(elem)
    if len(children) == 1:
        container = children[0]
        if container.tag in (_RDF_BAG, _RDF_SEQ):
            return [(li.text or "") for li in container.findall(_RDF_LI)]
        if container.tag == _RDF_ALT:
            return _pick_alt_lang(container)
        if container.tag == _RDF_DESCRIPTION:
            return _flatten_description(container)
    return (elem.text or "").strip()


def _pick_alt_lang(container: Element) -> str:
    """From an ``<rdf:Alt>`` container, pick ``xml:lang="x-default"`` if
    present, else fall back to the first ``<rdf:li>``. Empty container
    yields empty string.
    """
    items = container.findall(_RDF_LI)
    for li in items:
        if li.get(_XML_LANG) == "x-default":
            return li.text or ""
    if items:
        return items[0].text or ""
    return ""


def _flatten_description(desc: Element) -> dict[str, Any]:
    """Flatten a structured ``<rdf:Description>`` into a dict of its
    fields. Used for XMP properties whose value is a structured record —
    e.g. ``exif:Flash`` carrying ``Fired`` / ``Mode`` / ``Function`` —
    rather than a scalar / Bag / Seq / Alt.

    Sub-fields from namespaces outside :data:`_NAMESPACE_PREFIXES` are
    dropped (same policy as the top-level walker; ``rdf:about`` and other
    RDF bookkeeping are filtered out by the namespace check since the RDF
    namespace isn't in the friendly map). Sub-fields are keyed by their
    local name only — the parent property's namespace already qualifies
    the structured value, so re-prefixing the inner fields would be noisy.
    """
    result: dict[str, Any] = {}
    for attr_qname, attr_value in desc.attrib.items():
        uri, local = _strip_namespace(attr_qname)
        if uri is None or uri not in _NAMESPACE_PREFIXES:
            continue
        result[local] = attr_value
    for prop in desc:
        uri, local = _strip_namespace(prop.tag)
        if uri is None or uri not in _NAMESPACE_PREFIXES:
            continue
        result[local] = _flatten_value(prop)
    return result


def _flatten_xmp(root: Element) -> tuple[XmpData, list[str]]:
    """Walk an ``x:xmpmeta`` (or bare ``rdf:RDF``) tree and produce the
    flattened ``{prefix: {field: value}}`` dict for known namespaces.

    Multiple ``<rdf:Description>`` blocks merge into the same prefix dict.
    When two blocks define the same ``(prefix, field)`` pair, the later
    definition wins (XMP allows duplicates and we trust the file's order)
    — but the overwrite surfaces a warning so it's visible to consumers
    who care.

    Returns ``(data, warnings)``. ``warnings`` collects two kinds of
    visibility signal:

    - **Dropped vendor namespaces.** Properties from URIs outside
      :data:`_NAMESPACE_PREFIXES` are dropped from ``data``; their
      namespace URIs are accumulated and surfaced as one batched warning
      so a user expecting e.g. Lightroom-develop XMP knows the data was
      present but unsurfaced.
    - **Duplicate-field overwrites.** When a later ``<rdf:Description>``
      overwrites an earlier definition of the same ``(prefix, field)``,
      the pairs are accumulated and surfaced as one batched warning so
      the silent-overwrite policy is visible.

    Bookkeeping namespaces (RDF itself, ``xml:lang``, etc.) are excluded
    from the dropped-namespace count so the warning isn't noise.
    """
    rdf = root if root.tag == _RDF_RDF else root.find(_RDF_RDF)
    if rdf is None:
        return {}, []

    data: XmpData = {}
    dropped_namespaces: set[str] = set()
    overwritten: set[tuple[str, str]] = set()

    def _set_field(prefix: str, local: str, value: Any) -> None:
        """Set ``data[prefix][local] = value``, tracking overwrites for the
        duplicate-field warning. Inner closure so overwrites accumulate
        without threading another collection through the loop."""
        bucket = data.setdefault(prefix, {})
        if local in bucket:
            overwritten.add((prefix, local))
        bucket[local] = value

    for desc in rdf.findall(_RDF_DESCRIPTION):
        # Attribute form: <rdf:Description dc:title="foo" .../>
        for attr_qname, attr_value in desc.attrib.items():
            uri, local = _strip_namespace(attr_qname)
            prefix = _NAMESPACE_PREFIXES.get(uri) if uri else None
            if prefix is None:
                if uri is not None and uri not in _BOOKKEEPING_NAMESPACES:
                    dropped_namespaces.add(uri)
                continue
            _set_field(prefix, local, attr_value)
        # Element form: <dc:title>...</dc:title> with optional structured value
        for prop in desc:
            uri, local = _strip_namespace(prop.tag)
            prefix = _NAMESPACE_PREFIXES.get(uri) if uri else None
            if prefix is None:
                if uri is not None and uri not in _BOOKKEEPING_NAMESPACES:
                    dropped_namespaces.add(uri)
                continue
            _set_field(prefix, local, _flatten_value(prop))

    warnings: list[str] = []
    if dropped_namespaces:
        # Sort so the warning is deterministic across runs (set iteration
        # order would otherwise vary between processes).
        joined = ", ".join(sorted(dropped_namespaces))
        count = len(dropped_namespaces)
        warnings.append(f"Dropped properties from {count} unsurfaced namespace(s): {joined}")
    if overwritten:
        # Same deterministic-ordering rationale as the dropped-namespace warning.
        joined = ", ".join(sorted(f"{prefix}:{local}" for prefix, local in overwritten))
        count = len(overwritten)
        warnings.append(
            f"Later <rdf:Description> overwrote {count} previously-set field(s): {joined}"
        )
    return data, warnings


def _has_extended_xmp(root: Element) -> bool:
    """Return True when the tree contains an ``xmpNote:HasExtendedXMP``
    attribute or element. The marker can appear either form; both are
    legal per the XMP spec.
    """
    rdf = root if root.tag == _RDF_RDF else root.find(_RDF_RDF)
    if rdf is None:
        return False
    for desc in rdf.findall(_RDF_DESCRIPTION):
        if _HAS_EXTENDED_XMP in desc.attrib:
            return True
        if desc.find(_HAS_EXTENDED_XMP) is not None:
            return True
    return False


class XmpExtractor(Extractor[XmpData]):
    """Read XMP metadata from a JPEG or PNG; flatten RDF to a nested dict.

    No XMP packet → empty payload, no errors (this is normal, not a failure).
    Unsupported host format (TIFF, GIF, etc.) → empty payload + one warning.
    Malformed XML or XXE / DTD attempt → empty payload + one error string;
    extraction never raises out of this method except via the boundary
    contracts (:class:`~pixel_probe.exceptions.MissingFileError`,
    :class:`~pixel_probe.exceptions.FileTooLargeError`).
    """

    name = "xmp"
    # Annotated explicitly for the same reason as the other extractors —
    # mypy strict otherwise infers ``type[dict]`` and rejects the variance
    # against the ABC's parameterized type.
    payload_type: type[XmpData] = dict

    def extract(self, path: Path) -> ExtractorResult[XmpData]:
        if not path.is_file():
            raise MissingFileError(f"Not a file: {path}")
        size = path.stat().st_size
        if size > MAX_FILE_SIZE_BYTES:
            raise FileTooLargeError(f"{path} is {size:,} bytes; max is {MAX_FILE_SIZE_BYTES:,}")

        # XMP packets live in the first ~16 KB after a JPEG SOI or after the
        # IHDR chunk in a PNG. The whole-file load is wasteful for that
        # purpose but bounded by MAX_FILE_SIZE_BYTES (500 MB). Streaming a
        # window would be more memory-efficient but adds complexity v0.1
        # doesn't need — revisit if profiling shows pressure.
        raw = path.read_bytes()
        data: XmpData = {}

        if not raw.startswith((_JPEG_SOI, _PNG_SIGNATURE)):
            # Zero-data failure: XMP produces nothing for non-JPEG/PNG inputs.
            # data=None + error per ADR 0011's normalization.
            return ExtractorResult(
                self.name,
                errors=("File is not a JPEG or PNG; XMP v0.1 supports JPEG/PNG only",),
            )

        packet, locator_warnings, locator_errors = _find_xmp_packet(raw)
        if packet is None:
            # Two sub-cases:
            #   1. locator_errors empty → no XMP packet found in a valid file.
            #      Success-but-empty: data={}, no errors. Same shape as a JPEG
            #      with no XMP block.
            #   2. locator_errors non-empty → walker found an XMP-shaped chunk
            #      but couldn't decode it (compressed-iTXt malformed, bomb,
            #      etc.). Zero-data failure: data=None per ADR 0011.
            if locator_errors:
                return ExtractorResult(
                    self.name,
                    warnings=tuple(locator_warnings),
                    errors=tuple(locator_errors),
                )
            return ExtractorResult(
                self.name,
                data,
                warnings=tuple(locator_warnings),
            )

        try:
            root = _safe_fromstring(packet)
        except DefusedXmlException as e:
            # XXE / DTD / external-entity attempt blocked by defusedxml.
            # Zero-data failure (the security gate rejected the packet);
            # data=None + error per ADR 0011's normalization.
            return ExtractorResult(
                self.name,
                warnings=tuple(locator_warnings),
                errors=(
                    *locator_errors,
                    f"XMP rejected for security: {type(e).__name__}: {e}",
                ),
            )
        except ParseError as e:
            # Zero-data failure: malformed XML; data=None per ADR 0011.
            return ExtractorResult(
                self.name,
                warnings=tuple(locator_warnings),
                errors=(
                    *locator_errors,
                    f"XMP parse error: {type(e).__name__}: {e}",
                ),
            )

        warnings: list[str] = list(locator_warnings)
        # Extended XMP — an additional packet referenced by xmpNote:HasExtendedXMP
        # is out of scope for v1. Surface a warning when the marker is present
        # so users know the main packet is what they're seeing.
        if _has_extended_xmp(root):
            warnings.append(
                "Extended XMP detected (xmpNote:HasExtendedXMP); only the main packet is parsed"
            )

        flattened, flatten_warnings = _flatten_xmp(root)
        warnings.extend(flatten_warnings)
        return ExtractorResult(
            self.name,
            flattened,
            warnings=tuple(warnings),
            errors=tuple(locator_errors),
        )
