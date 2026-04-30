"""XMP metadata extraction via :mod:`defusedxml`.

XMP (Extensible Metadata Platform) is XML data embedded in image files.
v0.1 supports two host formats:

- **JPEG** — APP1 segment (``0xFFE1``) whose payload begins with the ASCII
  signature ``"http://ns.adobe.com/xap/1.0/\\0"``. Extended XMP via
  ``xmpNote:HasExtendedXMP`` is documented out of scope; we warn if
  detected and parse only the main packet.
- **PNG** — uncompressed iTXt chunk with keyword ``XML:com.adobe.xmp``.
  Compressed iTXt is rare for XMP and out of scope (we return ``None``
  rather than carrying a zlib dep just for this case).

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
  silently dropped (the XMP namespace zoo has hundreds of vendor-specific
  schemas; surfacing them all would be noise).
"""

from __future__ import annotations

# ParseError comes from the stdlib but is what defusedxml raises for malformed
# XML. Importing the stdlib symbol keeps the typing edge sharp; defusedxml
# only adds DTD/entity rejection on top of the stdlib parser.
from pathlib import Path
from typing import Any, Final
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from pixel_probe.exceptions import FileTooLargeError, MissingFileError

from .base import Extractor, ExtractorResult
from .file_info import MAX_FILE_SIZE_BYTES

__all__ = ["XmpData", "XmpExtractor"]

#: Type alias for the XMP payload. Two-level dict: ``{prefix: {field: value}}``.
#: Values are ``str`` or ``list[str]`` after RDF flattening.
XmpData = dict[str, Any]

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
#: silently dropped — XMP has a long tail of vendor-specific namespaces and
#: surfacing them all as e.g. ``ns0_creator`` would be noise.
_NAMESPACE_PREFIXES: Final[dict[str, str]] = {
    "http://purl.org/dc/elements/1.1/": "dc",
    "http://ns.adobe.com/xap/1.0/": "xmp",
    "http://ns.adobe.com/photoshop/1.0/": "photoshop",
    "http://ns.adobe.com/exif/1.0/": "exif",
    "http://ns.adobe.com/tiff/1.0/": "tiff",
    "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/": "Iptc4xmpCore",
}


def _find_xmp_packet(raw: bytes) -> bytes | None:
    """Locate the XMP packet bytes in a JPEG or PNG byte stream.

    Dispatches on file signature. Returns ``None`` when the host format is
    unsupported or no XMP packet is present.
    """
    if raw.startswith(_JPEG_SOI):
        return _find_xmp_packet_jpeg(raw)
    if raw.startswith(_PNG_SIGNATURE):
        return _find_xmp_packet_png(raw)
    return None


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


def _find_xmp_packet_png(raw: bytes) -> bytes | None:
    """Walk PNG chunks looking for an iTXt chunk with the XMP keyword.

    PNG chunk layout: ``<4-byte BE length> <4-byte type> <data> <4-byte CRC>``.
    The length field describes ``data`` only — the type field and CRC are
    fixed-size. We bounds-check before each chunk read.

    Compressed iTXt is detected (compression flag = 1) and skipped with no
    result — not bothering to decompress for this rare case.
    """
    offset = len(_PNG_SIGNATURE)
    n = len(raw)
    # Minimum chunk shape: 4 length + 4 type + 4 CRC = 12 bytes.
    while offset + 12 <= n:
        length = int.from_bytes(raw[offset : offset + 4], "big")
        chunk_type = raw[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        # data_end + 4 because the CRC follows the data.
        if data_end + 4 > n:
            return None
        if chunk_type == _PNG_ITXT_TYPE:
            packet = _read_itxt_xmp(raw[data_start:data_end])
            if packet is not None:
                return packet
        offset = data_end + 4  # next chunk: skip CRC
    return None


def _read_itxt_xmp(data: bytes) -> bytes | None:
    """Parse an iTXt chunk payload; return its text only when keyword is XMP.

    iTXt structure: ``keyword \\0 compression-flag(1) compression-method(1)
    language-tag \\0 translated-keyword \\0 text``. We bounds-check each
    NUL-terminated section and bail on truncation.
    """
    nul = data.find(b"\x00")
    if nul == -1 or data[:nul] != _PNG_XMP_KEYWORD:
        return None
    # Need at least: nul + compression-flag + compression-method = nul + 3
    if len(data) < nul + 3:
        return None
    compression_flag = data[nul + 1]
    if compression_flag == 1:
        # Compressed XMP — skip rather than carry a zlib dep for this rare case.
        return None
    # Skip language tag (\0-terminated) and translated keyword (\0-terminated).
    lang_start = nul + 3
    lang_end = data.find(b"\x00", lang_start)
    if lang_end == -1:
        return None
    tkw_end = data.find(b"\x00", lang_end + 1)
    if tkw_end == -1:
        return None
    return data[tkw_end + 1 :]


def _strip_namespace(qname: str) -> tuple[str | None, str]:
    """Split ElementTree's ``{uri}local`` form into ``(uri, local)``.

    Elements / attributes without a namespace return ``(None, qname)``.
    """
    if qname.startswith("{"):
        end = qname.find("}")
        if end > 0:
            return qname[1:end], qname[end + 1 :]
    return None, qname


def _flatten_value(elem: Element) -> str | list[str]:
    """Convert an XMP property element to a Python primitive.

    - ``<rdf:Bag>`` / ``<rdf:Seq>`` → ``list[str]`` (in document order)
    - ``<rdf:Alt>`` → the ``x-default`` lang's text, or first ``rdf:li``
      if no ``x-default`` is present
    - anything else → the element's stripped text content (empty string
      when absent)
    """
    children = list(elem)
    if len(children) == 1:
        container = children[0]
        if container.tag in (_RDF_BAG, _RDF_SEQ):
            return [(li.text or "") for li in container.findall(_RDF_LI)]
        if container.tag == _RDF_ALT:
            return _pick_alt_lang(container)
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


def _flatten_xmp(root: Element) -> XmpData:
    """Walk an ``x:xmpmeta`` (or bare ``rdf:RDF``) tree and produce the
    flattened ``{prefix: {field: value}}`` dict for known namespaces.

    Multiple ``<rdf:Description>`` blocks merge into the same prefix dict;
    later definitions overwrite earlier ones (XMP allows duplicates and
    we trust the file's order — the first or last is policy, not
    correctness).
    """
    rdf = root if root.tag == _RDF_RDF else root.find(_RDF_RDF)
    if rdf is None:
        return {}

    data: XmpData = {}
    for desc in rdf.findall(_RDF_DESCRIPTION):
        # Attribute form: <rdf:Description dc:title="foo" .../>
        for attr_qname, attr_value in desc.attrib.items():
            uri, local = _strip_namespace(attr_qname)
            prefix = _NAMESPACE_PREFIXES.get(uri) if uri else None
            if prefix is None:
                continue
            data.setdefault(prefix, {})[local] = attr_value
        # Element form: <dc:title>...</dc:title> with optional structured value
        for prop in desc:
            uri, local = _strip_namespace(prop.tag)
            prefix = _NAMESPACE_PREFIXES.get(uri) if uri else None
            if prefix is None:
                continue
            data.setdefault(prefix, {})[local] = _flatten_value(prop)
    return data


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
        if path.stat().st_size > MAX_FILE_SIZE_BYTES:
            raise FileTooLargeError(
                f"{path} is {path.stat().st_size:,} bytes; max is {MAX_FILE_SIZE_BYTES:,}"
            )

        raw = path.read_bytes()
        data: XmpData = {}

        if not raw.startswith((_JPEG_SOI, _PNG_SIGNATURE)):
            return ExtractorResult(
                self.name,
                data,
                warnings=("File is not a JPEG or PNG; XMP v0.1 supports JPEG/PNG only",),
            )

        packet = _find_xmp_packet(raw)
        if packet is None:
            return ExtractorResult(self.name, data)

        try:
            root = _safe_fromstring(packet)
        except DefusedXmlException as e:
            # XXE / DTD / external-entity attempt blocked by defusedxml.
            # Surface as an error so the security gate is visible to callers.
            return ExtractorResult(
                self.name,
                data,
                errors=(f"XMP rejected for security: {type(e).__name__}: {e}",),
            )
        except ParseError as e:
            return ExtractorResult(
                self.name,
                data,
                errors=(f"XMP parse error: {type(e).__name__}: {e}",),
            )

        warnings: tuple[str, ...] = ()
        # Extended XMP — an additional packet referenced by xmpNote:HasExtendedXMP
        # is out of scope for v1. Surface a warning when the marker is present
        # so users know the main packet is what they're seeing.
        if _has_extended_xmp(root):
            warnings = (
                "Extended XMP detected (xmpNote:HasExtendedXMP); only the main packet is parsed",
            )

        return ExtractorResult(self.name, _flatten_xmp(root), warnings=warnings)


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
