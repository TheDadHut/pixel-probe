"""XMP extractor tests — RDF flattening, host-format dispatch, XXE security gate.

The XXE test (:func:`test_xxe_payload_blocked`) is the load-bearing one for
portfolio signal: it confirms that ``defusedxml`` actually rejects the XXE
payload we feed it. If this test ever passes by silently resolving the
external entity, the security guarantee in ADR 0003 is broken — that's
the regression we want to catch.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

# defusedxml.fromstring is identical to xml.etree.ElementTree.fromstring for
# safe inputs; using it in tests avoids ruff's S314 (untrusted-XML) lint rule
# while keeping the assertions identical. The literal XML strings here are
# obviously safe — but the rule fires uniformly, and the safer call is free.
from defusedxml.ElementTree import fromstring

from pixel_probe.core.analyzer import Analyzer
from pixel_probe.core.extractors.xmp import (
    _MAX_DECOMPRESSED_XMP_BYTES,
    XmpExtractor,
    _find_xmp_packet,
    _find_xmp_packet_jpeg,
    _find_xmp_packet_png,
    _flatten_description,
    _flatten_value,
    _flatten_xmp,
    _has_extended_xmp,
    _pick_alt_lang,
    _read_itxt_xmp,
    _strip_namespace,
)
from pixel_probe.exceptions import MissingFileError

from .conftest import fixture_path

# ---------------------------------------------------------------------------
# Integration: real fixtures
# ---------------------------------------------------------------------------


def test_parses_dc_title_alt() -> None:
    """``dc:title`` is encoded as ``<rdf:Alt>`` with language alternatives.
    The flattener picks ``x-default`` and surfaces it as a plain string.

    The fixture deliberately includes a ``vendor:OutOfScope`` element from
    an unsurfaced namespace to verify the dropped-namespace warning fires
    in real fixture loads — see ``test_unknown_namespace_dropped_with_warning``
    for the warning's exact shape."""
    result = XmpExtractor().extract(fixture_path("xmp_basic.jpg"))

    assert result.extractor_name == "xmp"
    assert result.errors == ()
    assert result.has_data
    assert result.data["dc"]["title"] == "XMP Sample Title"


def test_keywords_as_list_from_rdf_bag() -> None:
    """``dc:subject`` (Bag of li elements) flattens to ``list[str]`` in
    document order — the load-bearing repeatable-property contract."""
    result = XmpExtractor().extract(fixture_path("xmp_basic.jpg"))

    assert result.data["dc"]["subject"] == ["nature", "landscape", "sample"]


def test_creator_as_list_from_rdf_seq() -> None:
    """``rdf:Seq`` flattens to a list the same way ``rdf:Bag`` does — Seq
    semantics imply order, but at the storage layer we treat them
    identically (list preserves order anyway)."""
    result = XmpExtractor().extract(fixture_path("xmp_basic.jpg"))

    assert result.data["dc"]["creator"] == ["Alice Author"]


def test_simple_text_properties() -> None:
    """``xmp:CreatorTool`` and ``photoshop:Headline`` are simple text —
    surfaced as scalar strings under their friendly prefix."""
    result = XmpExtractor().extract(fixture_path("xmp_basic.jpg"))

    assert result.data["xmp"]["CreatorTool"] == "pixel-probe-fixtures"
    assert result.data["photoshop"]["Headline"] == "Sample headline"


def test_unknown_namespace_dropped_with_warning() -> None:
    """Properties from namespaces not in :data:`_NAMESPACE_PREFIXES` are
    dropped from ``data`` — the fixture deliberately includes a
    ``vendor:OutOfScope`` element to verify this — but the namespace URI
    surfaces in the warnings list so the omission is visible to consumers
    rather than being a silent miss."""
    result = XmpExtractor().extract(fixture_path("xmp_basic.jpg"))

    assert "vendor" not in result.data
    # Walk every value to confirm the marker text isn't smuggled in
    # under any other namespace.
    for ns_data in result.data.values():
        for value in ns_data.values():
            assert "should be dropped" not in (value if isinstance(value, str) else " ".join(value))
    # The warning is the user-visible signal that something was dropped.
    assert any("vendor/1.0/" in w for w in result.warnings)
    assert any("unsurfaced namespace" in w for w in result.warnings)


def test_returns_empty_for_image_without_xmp() -> None:
    """No XMP packet in the host file → empty data, no errors, no warnings.
    This is the normal case for un-tagged images."""
    result = XmpExtractor().extract(fixture_path("xmp_none.jpg"))

    assert result.data == {}
    assert result.errors == ()
    assert result.warnings == ()
    assert not result.has_data


def test_malformed_xml_returns_error_not_crash() -> None:
    """A truncated XMP packet must surface a single error string and not
    propagate the :class:`xml.etree.ElementTree.ParseError` out — the
    boundary contract for failure parses. Per ADR 0011, zero-data
    failure → ``data=None``."""
    result = XmpExtractor().extract(fixture_path("xmp_malformed.jpg"))

    assert result.data is None
    assert len(result.errors) == 1
    assert "ParseError" in result.errors[0] or "parse error" in result.errors[0].lower()


def test_png_path_parses_xmp() -> None:
    """End-to-end PNG dispatch: a PNG with an iTXt chunk carrying XMP is
    parsed identically to the JPEG path. Validates the per-format
    dispatch in :func:`_find_xmp_packet`."""
    result = XmpExtractor().extract(fixture_path("xmp_basic.png"))

    assert result.errors == ()
    assert result.data["dc"]["title"] == "PNG XMP Title"


def test_unsupported_format_returns_error() -> None:
    """A non-JPEG / non-PNG file (plain text fixture) returns ``data=None``
    + one error per ADR 0011. XMP produces zero data for unsupported host
    formats — that's a failure, not a warning. TIFF support and other
    host formats are out of scope for v0.1; the error makes that visible
    to the caller."""
    result = XmpExtractor().extract(fixture_path("not_an_image.txt"))

    assert result.data is None
    assert result.warnings == ()
    assert result.errors == ("File is not a JPEG or PNG; XMP v0.1 supports JPEG/PNG only",)


def test_missing_file_raises_typed_error(tmp_path: Path) -> None:
    """Same boundary contract as the other extractors: missing input is a
    :class:`MissingFileError` (a :class:`PixelProbeError` subclass)."""
    with pytest.raises(MissingFileError):
        XmpExtractor().extract(tmp_path / "does-not-exist.jpg")


def test_idempotent() -> None:
    """Same input → same output, every call. Pure I/O, no global state."""
    extractor = XmpExtractor()
    a = extractor.extract(fixture_path("xmp_basic.jpg"))
    b = extractor.extract(fixture_path("xmp_basic.jpg"))
    assert a.data == b.data
    assert a.errors == b.errors


def test_wired_into_default_analyzer() -> None:
    """The default factory must include XMP — end-to-end smoke that the
    extractor is reachable through the public ``Analyzer.default()`` API."""
    result = Analyzer.default().analyze(fixture_path("xmp_basic.jpg"))

    assert "xmp" in result.results
    assert result.results["xmp"].has_data
    assert result.results["xmp"].data["dc"]["title"] == "XMP Sample Title"


# ---------------------------------------------------------------------------
# Security: XXE / DTD rejection (load-bearing portfolio signal)
# ---------------------------------------------------------------------------


def test_xxe_payload_blocked(tmp_path: Path) -> None:
    """The flagship security test: an XMP packet carrying an XXE attack
    (external entity reference to a local file) must be rejected by
    :mod:`defusedxml` *before* any entity resolution. The result is an
    error string surfacing the security exception; no file contents
    leak into the parse output.

    If this test ever passes by silently resolving the entity (e.g. if
    someone swaps :func:`defusedxml.ElementTree.fromstring` for the
    stdlib version), it'll either contain leaked file contents or fail
    in a different way — the assertions here pin both the rejection
    *and* the no-leak property.
    """
    secret_target = tmp_path / "secret.txt"
    secret_target.write_text("SECRET_DATA_THAT_MUST_NOT_LEAK")

    xxe_packet = (
        b'<?xml version="1.0"?>'
        b"<!DOCTYPE foo ["
        b'  <!ENTITY xxe SYSTEM "file://' + str(secret_target).encode() + b'">'
        b"]>"
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b"<dc:title>&xxe;</dc:title>"
        b"</rdf:Description>"
        b"</rdf:RDF>"
        b"</x:xmpmeta>"
    )
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + xxe_packet
    length = len(payload) + 2
    app1 = b"\xff\xe1" + length.to_bytes(2, "big") + payload
    # Minimum-viable JPEG: SOI + APP1 + EOI. Real decoders won't decode it
    # to pixels, but our parser only walks segments.
    out = tmp_path / "xxe.jpg"
    out.write_bytes(b"\xff\xd8" + app1 + b"\xff\xd9")

    result = XmpExtractor().extract(out)

    # Security gate: defusedxml rejected the parse. Per ADR 0011,
    # zero-data failure → data=None.
    assert result.data is None
    assert len(result.errors) == 1
    assert "security" in result.errors[0].lower()

    # No-leak property: the file contents must not appear anywhere on the
    # result envelope. This is what fails loudest if the security guarantee
    # ever breaks.
    leak_marker = "SECRET_DATA_THAT_MUST_NOT_LEAK"
    assert leak_marker not in result.errors[0]
    for value in (result.warnings, result.errors):
        assert leak_marker not in repr(value)


def test_extended_xmp_warns(tmp_path: Path) -> None:
    """A file marked with ``xmpNote:HasExtendedXMP`` produces a warning
    (extended packets are out of scope for v1; we parse the main packet
    only)."""
    packet = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description rdf:about=""'
        b'  xmlns:xmpNote="http://ns.adobe.com/xmp/note/"'
        b'  xmpNote:HasExtendedXMP="abc123" />'
        b"</rdf:RDF>"
        b"</x:xmpmeta>"
    )
    full_payload = b"http://ns.adobe.com/xap/1.0/\x00" + packet
    length = len(full_payload) + 2
    app1 = b"\xff\xe1" + length.to_bytes(2, "big") + full_payload
    out = tmp_path / "ext.jpg"
    out.write_bytes(b"\xff\xd8" + app1 + b"\xff\xd9")

    result = XmpExtractor().extract(out)

    assert any("Extended XMP" in w for w in result.warnings)


def test_compressed_png_itxt_decompresses_end_to_end(tmp_path: Path) -> None:
    """End-to-end: a PNG with a compressed iTXt XMP chunk is decompressed
    via stdlib ``zlib`` and the inflated XMP parsed normally. Surfaces no
    warning in the success path — the user shouldn't care that the
    container happened to be compressed."""
    # Build a minimal PNG that's just signature + a compressed-iTXt chunk
    # (no IHDR — our walker doesn't validate chunk order, only chunk
    # structure, so this is enough to drive the path).
    real_xmp = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description rdf:about=""'
        b'  xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b"<dc:title>"
        b'<rdf:Alt><rdf:li xml:lang="x-default">Compressed XMP</rdf:li></rdf:Alt>'
        b"</dc:title>"
        b"</rdf:Description>"
        b"</rdf:RDF>"
        b"</x:xmpmeta>"
    )
    compressed_xmp = zlib.compress(real_xmp)
    payload = b"XML:com.adobe.xmp\x00\x01\x00\x00\x00" + compressed_xmp
    chunk = _build_png_chunk(b"iTXt", payload)
    out = tmp_path / "compressed.png"
    out.write_bytes(_PNG_SIGNATURE + chunk)

    result = XmpExtractor().extract(out)

    assert result.errors == ()
    assert result.warnings == ()
    assert result.data["dc"]["title"] == "Compressed XMP"


def test_compressed_png_itxt_malformed_surfaces_error(tmp_path: Path) -> None:
    """End-to-end: a PNG whose compressed iTXt body isn't a valid zlib
    stream surfaces an error on the result envelope rather than crashing
    or silently returning empty. Severity is error (not warning) — same
    severity as the malformed-XML path, since both are "we found a chunk
    but couldn't decode it"."""
    payload = b"XML:com.adobe.xmp\x00\x01\x00\x00\x00not zlib at all"
    chunk = _build_png_chunk(b"iTXt", payload)
    out = tmp_path / "malformed.png"
    out.write_bytes(_PNG_SIGNATURE + chunk)

    result = XmpExtractor().extract(out)

    # Per ADR 0011, zero-data failure → data=None.
    assert result.data is None
    assert result.warnings == ()
    assert any("Malformed compressed" in e for e in result.errors)


def test_compressed_png_itxt_bomb_surfaces_error(tmp_path: Path) -> None:
    """End-to-end zlib-bomb defense: a PNG with a tiny compressed iTXt that
    inflates past ``_MAX_DECOMPRESSED_XMP_BYTES`` is rejected without
    allocating the full output. Surfaces as an error so the security gate
    is visible to consumers, parallel to the malformed-XML path. Without
    this cap, a malicious PNG could trigger OOM during decompression."""
    bomb_payload = b"a" * (_MAX_DECOMPRESSED_XMP_BYTES + 1024)
    compressed = zlib.compress(bomb_payload)
    payload = b"XML:com.adobe.xmp\x00\x01\x00\x00\x00" + compressed
    chunk = _build_png_chunk(b"iTXt", payload)
    out = tmp_path / "bomb.png"
    out.write_bytes(_PNG_SIGNATURE + chunk)

    result = XmpExtractor().extract(out)

    # Per ADR 0011, zero-data failure → data=None.
    assert result.data is None
    assert result.warnings == ()
    assert any("decompression bomb" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# Helper unit tests — _strip_namespace
# ---------------------------------------------------------------------------


def test_strip_namespace_qualified() -> None:
    """``{uri}local`` form splits into ``(uri, local)``."""
    assert _strip_namespace("{http://example.com/}foo") == ("http://example.com/", "foo")


def test_strip_namespace_unqualified() -> None:
    """A bare local name has no URI."""
    assert _strip_namespace("foo") == (None, "foo")


def test_strip_namespace_malformed_brace() -> None:
    """A leading ``{`` without a closing ``}`` is treated as a literal name —
    don't crash, don't try to invent a namespace."""
    assert _strip_namespace("{noend") == (None, "{noend")


# ---------------------------------------------------------------------------
# Helper unit tests — _flatten_value / _pick_alt_lang
# ---------------------------------------------------------------------------


def test_flatten_value_simple_text() -> None:
    """A property with no children flattens to its text content (stripped)."""
    elem = fromstring("<x>  hello  </x>")
    assert _flatten_value(elem) == "hello"


def test_flatten_value_simple_text_empty() -> None:
    """An empty-text element flattens to the empty string."""
    elem = fromstring("<x />")
    assert _flatten_value(elem) == ""


def test_flatten_value_bag_to_list() -> None:
    """``rdf:Bag`` of ``rdf:li`` flattens to ``list[str]`` in document order."""
    xml = (
        '<root xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        "<rdf:Bag><rdf:li>a</rdf:li><rdf:li>b</rdf:li></rdf:Bag>"
        "</root>"
    )
    elem = fromstring(xml)
    assert _flatten_value(elem) == ["a", "b"]


def test_flatten_value_seq_to_list() -> None:
    """``rdf:Seq`` follows the same shape as Bag — both flatten to a list
    (Seq's order semantics are preserved naturally)."""
    xml = (
        '<root xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        "<rdf:Seq><rdf:li>first</rdf:li><rdf:li>second</rdf:li></rdf:Seq>"
        "</root>"
    )
    elem = fromstring(xml)
    assert _flatten_value(elem) == ["first", "second"]


def test_flatten_value_alt_picks_x_default() -> None:
    """``rdf:Alt`` with an ``xml:lang="x-default"`` entry picks that one,
    not the first."""
    xml = (
        '<root xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        "<rdf:Alt>"
        '<rdf:li xml:lang="fr">French first</rdf:li>'
        '<rdf:li xml:lang="x-default">Default</rdf:li>'
        "</rdf:Alt>"
        "</root>"
    )
    elem = fromstring(xml)
    assert _flatten_value(elem) == "Default"


def test_flatten_value_alt_falls_back_to_first_when_no_default() -> None:
    """``rdf:Alt`` without an ``x-default`` entry falls back to the first
    ``<rdf:li>``."""
    xml = (
        '<root xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        "<rdf:Alt>"
        '<rdf:li xml:lang="fr">First lang</rdf:li>'
        '<rdf:li xml:lang="de">Second lang</rdf:li>'
        "</rdf:Alt>"
        "</root>"
    )
    elem = fromstring(xml)
    assert _flatten_value(elem) == "First lang"


def test_pick_alt_lang_empty_alt_returns_empty() -> None:
    """A degenerate ``rdf:Alt`` with no ``rdf:li`` children returns the
    empty string — caller still gets a string, not None."""
    xml = '<rdf:Alt xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'
    elem = fromstring(xml)
    assert _pick_alt_lang(elem) == ""


def test_pick_alt_lang_first_li_with_no_text() -> None:
    """A first-fallback ``<rdf:li>`` with no text content returns empty
    string, not None — same string-only contract."""
    xml = (
        '<rdf:Alt xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:li xml:lang="fr"></rdf:li>'
        "</rdf:Alt>"
    )
    elem = fromstring(xml)
    assert _pick_alt_lang(elem) == ""


def test_flatten_value_two_children_treated_as_simple() -> None:
    """A property with multiple non-container children doesn't match any
    of the structured-value rules — falls through to text-content extraction."""
    xml = "<x>some text<a/><b/></x>"
    elem = fromstring(xml)
    # Two children → not Bag/Seq/Alt; flatten to (text or empty).
    assert _flatten_value(elem) == "some text"


def test_flatten_value_single_non_container_child_falls_through_to_text() -> None:
    """A property with exactly one child that's *not* Bag/Seq/Alt/Description
    falls through every structured-value rule and lands on the text-content
    fallback. Covers the branch from the Description check straight to the
    simple-text return."""
    xml = "<x>parent text<nested>child text</nested></x>"
    elem = fromstring(xml)
    # The single child isn't an RDF container — flattens to parent's text.
    assert _flatten_value(elem) == "parent text"


def test_flatten_value_nested_description_to_dict() -> None:
    """A property whose child is an ``<rdf:Description>`` (an XMP structured
    value, e.g. ``exif:Flash``) flattens to a ``dict`` of the inner
    Description's fields. Without this, the structured payload was silently
    dropped — the value became an empty string."""
    xml = (
        '<exif:Flash xmlns:exif="http://ns.adobe.com/exif/1.0/"'
        '            xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description exif:Fired="True" exif:Mode="0" exif:Function="False"/>'
        "</exif:Flash>"
    )
    elem = fromstring(xml)
    flat = _flatten_value(elem)
    assert isinstance(flat, dict)
    assert flat == {"Fired": "True", "Mode": "0", "Function": "False"}


def test_flatten_description_drops_unknown_namespace_attributes() -> None:
    """A structured Description with attributes from a namespace not in our
    friendly map drops those fields silently — same policy as the top-level
    walker. Reuses the disjointness with rdf bookkeeping (rdf:about etc.)
    since RDF isn't in the friendly map either."""
    xml = (
        '<rdf:Description xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
        '                 xmlns:exif="http://ns.adobe.com/exif/1.0/"'
        '                 xmlns:vendor="http://example.com/vendor/1.0/"'
        '                 rdf:about=""'
        '                 exif:Fired="True"'
        '                 vendor:Internal="dropped" />'
    )
    elem = fromstring(xml)
    assert _flatten_description(elem) == {"Fired": "True"}


def test_flatten_description_recurses_into_element_form() -> None:
    """Element-form sub-fields inside a structured Description are
    flattened the same way attribute-form ones are. Recurses into
    ``_flatten_value`` for each child, so deeper structured values
    (rare) flatten correctly."""
    xml = (
        '<rdf:Description xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
        '                 xmlns:exif="http://ns.adobe.com/exif/1.0/">'
        "<exif:Fired>True</exif:Fired>"
        "<exif:Mode>0</exif:Mode>"
        "</rdf:Description>"
    )
    elem = fromstring(xml)
    assert _flatten_description(elem) == {"Fired": "True", "Mode": "0"}


def test_flatten_description_drops_unknown_namespace_elements() -> None:
    """Element-form sub-fields from a namespace not in our friendly map
    are dropped — same policy as the attribute-form case but covering
    the second loop (``for prop in desc``)."""
    xml = (
        '<rdf:Description xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
        '                 xmlns:exif="http://ns.adobe.com/exif/1.0/"'
        '                 xmlns:vendor="http://example.com/vendor/1.0/">'
        "<exif:Fired>True</exif:Fired>"
        "<vendor:Internal>dropped</vendor:Internal>"
        "</rdf:Description>"
    )
    elem = fromstring(xml)
    assert _flatten_description(elem) == {"Fired": "True"}


def test_flatten_description_empty_returns_empty_dict() -> None:
    """A bare ``<rdf:Description/>`` with no attributes or children
    flattens to an empty dict — defensive for spec-legal but unusual
    structured-property shapes."""
    xml = '<rdf:Description xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" />'
    elem = fromstring(xml)
    assert _flatten_description(elem) == {}


def test_flatten_description_in_real_xmp_packet(tmp_path: Path) -> None:
    """End-to-end: a full XMP packet with a structured ``exif:Flash``
    surfaces the structured value as a dict on the result envelope.
    Validates the full path from JPEG segment walker → defusedxml parse →
    ``_flatten_xmp`` → ``_flatten_value`` → ``_flatten_description``."""
    rdf_body = (
        '<rdf:Description rdf:about=""'
        '  xmlns:exif="http://ns.adobe.com/exif/1.0/">'
        "<exif:Flash>"
        '<rdf:Description exif:Fired="True" exif:Mode="0"/>'
        "</exif:Flash>"
        "</rdf:Description>"
    )
    packet = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        + rdf_body.encode()
        + b"</rdf:RDF>"
        b"</x:xmpmeta>"
    )
    full_payload = b"http://ns.adobe.com/xap/1.0/\x00" + packet
    length = len(full_payload) + 2
    app1 = b"\xff\xe1" + length.to_bytes(2, "big") + full_payload
    out = tmp_path / "structured.jpg"
    out.write_bytes(b"\xff\xd8" + app1 + b"\xff\xd9")

    result = XmpExtractor().extract(out)

    assert result.errors == ()
    assert result.data["exif"]["Flash"] == {"Fired": "True", "Mode": "0"}


# ---------------------------------------------------------------------------
# Helper unit tests — _flatten_xmp
# ---------------------------------------------------------------------------


def test_flatten_xmp_attributes_on_description() -> None:
    """Attribute-form properties on ``<rdf:Description>`` (e.g.
    ``<rdf:Description dc:title="foo"/>``) are surfaced the same as
    element-form properties."""
    xml = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about=""'
        '  xmlns:dc="http://purl.org/dc/elements/1.1/"'
        '  dc:title="Attribute form" />'
        "</rdf:RDF>"
        "</x:xmpmeta>"
    )
    root = fromstring(xml)
    data, warnings = _flatten_xmp(root)
    assert data == {"dc": {"title": "Attribute form"}}
    assert warnings == []


def test_flatten_xmp_bare_rdf_root_works() -> None:
    """An XMP packet that's just ``<rdf:RDF>`` directly (without the outer
    ``<x:xmpmeta>`` wrapper) still flattens. Some encoders skip the
    wrapper; we shouldn't be picky."""
    xml = (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about=""'
        '  xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>Bare RDF</dc:title>"
        "</rdf:Description>"
        "</rdf:RDF>"
    )
    root = fromstring(xml)
    data, warnings = _flatten_xmp(root)
    assert data == {"dc": {"title": "Bare RDF"}}
    assert warnings == []


def test_flatten_xmp_no_rdf_root_returns_empty() -> None:
    """A tree with neither ``rdf:RDF`` nor a top-level ``rdf:RDF`` child
    yields empty data — defensive for malformed-but-parseable XML."""
    xml = "<root><meta>no rdf here</meta></root>"
    root = fromstring(xml)
    assert _flatten_xmp(root) == ({}, [])


def test_flatten_xmp_unknown_attribute_namespace_dropped_with_warning() -> None:
    """An attribute from a vendor namespace not in our prefix map is
    dropped from the data dict, but the namespace URI surfaces in the
    warnings list — visible to consumers expecting that field. Same
    pattern for element-form drops."""
    xml = (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about=""'
        '  xmlns:dc="http://purl.org/dc/elements/1.1/"'
        '  xmlns:vendor="http://example.com/vendor/1.0/"'
        '  dc:title="Real title"'
        '  vendor:OutOfScope="ignored" />'
        "</rdf:RDF>"
    )
    root = fromstring(xml)
    data, warnings = _flatten_xmp(root)
    assert data == {"dc": {"title": "Real title"}}
    assert len(warnings) == 1
    assert "vendor/1.0/" in warnings[0]
    assert "1 unsurfaced namespace" in warnings[0]


def test_flatten_xmp_bookkeeping_namespaces_excluded_from_warning() -> None:
    """RDF / XML / xmpNote bookkeeping namespaces don't generate the
    dropped-namespace warning — they don't carry user metadata that
    would have been surfaced under a friendly prefix. Only namespaces
    that *would* have been useful (vendor / extension schemas) trigger
    the warning. This case covers attribute-form bookkeeping; the
    element-form variant is exercised below."""
    xml = (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about=""'
        '  xmlns:dc="http://purl.org/dc/elements/1.1/"'
        '  xmlns:xmpNote="http://ns.adobe.com/xmp/note/"'
        '  dc:title="Clean title"'
        '  xmpNote:HasExtendedXMP="abc" />'
        "</rdf:RDF>"
    )
    root = fromstring(xml)
    data, warnings = _flatten_xmp(root)
    assert data == {"dc": {"title": "Clean title"}}
    # rdf:about and xmpNote:HasExtendedXMP are bookkeeping; warning stays empty.
    assert warnings == []


def test_flatten_xmp_duplicate_field_overwrite_surfaces_warning() -> None:
    """Two ``<rdf:Description>`` blocks both defining ``dc:title`` →
    later wins, but the overwrite surfaces a warning so the silent-
    overwrite policy is visible. Real-world XMP rarely has duplicates
    (Description blocks split by namespace), so this fires only on
    pathological input — but when it does, the warning is the user's
    only signal that they're seeing one of two definitions."""
    xml = (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about=""'
        '  xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>First definition</dc:title>"
        "</rdf:Description>"
        '<rdf:Description rdf:about=""'
        '  xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>Second definition (wins)</dc:title>"
        "</rdf:Description>"
        "</rdf:RDF>"
    )
    root = fromstring(xml)
    data, warnings = _flatten_xmp(root)
    assert data == {"dc": {"title": "Second definition (wins)"}}
    assert any("dc:title" in w for w in warnings)
    assert any("overwrote" in w for w in warnings)


def test_flatten_xmp_bookkeeping_element_excluded_from_warning() -> None:
    """Element-form parity for the bookkeeping-namespace exclusion.
    A child element from ``xmpNote`` (the ExtendedXMP marker carrier)
    should not surface a dropped-namespace warning — same policy as
    the attribute-form case."""
    xml = (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about=""'
        '  xmlns:dc="http://purl.org/dc/elements/1.1/"'
        '  xmlns:xmpNote="http://ns.adobe.com/xmp/note/">'
        "<dc:title>Clean</dc:title>"
        "<xmpNote:HasExtendedXMP>abc</xmpNote:HasExtendedXMP>"
        "</rdf:Description>"
        "</rdf:RDF>"
    )
    root = fromstring(xml)
    data, warnings = _flatten_xmp(root)
    assert data == {"dc": {"title": "Clean"}}
    assert warnings == []


# ---------------------------------------------------------------------------
# Helper unit tests — _find_xmp_packet (host-format dispatch)
# ---------------------------------------------------------------------------


def test_find_xmp_packet_unknown_format_returns_none() -> None:
    """Bytes that match neither JPEG SOI nor the PNG signature → ``(None, [], [])``."""
    assert _find_xmp_packet(b"GIF89a...some random bytes") == (None, [], [])
    assert _find_xmp_packet(b"") == (None, [], [])


def test_find_xmp_packet_jpeg_no_xmp_returns_none() -> None:
    """A JPEG with no XMP segment → None (not an error, just absent)."""
    jpeg = b"\xff\xd8" + b"\xff\xd9"  # SOI + EOI, no segments
    assert _find_xmp_packet_jpeg(jpeg) is None


def test_find_xmp_packet_jpeg_handles_truncated_length() -> None:
    """A truncated APP segment doesn't crash the JPEG packet finder."""
    truncated = b"\xff\xd8\xff\xe1" + (9999).to_bytes(2, "big") + b"abc"
    assert _find_xmp_packet_jpeg(truncated) is None


def test_find_xmp_packet_jpeg_skips_standalone_markers() -> None:
    """Restart markers (``RST0..RST7``) appear with no length payload —
    walker advances by 2 bytes without yielding."""
    # SOI + RST0 + APP1(XMP) + EOI, where APP1 carries a real XMP packet.
    minimal_xmp = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" />'
        b"</x:xmpmeta>"
    )
    sig_and_packet = b"http://ns.adobe.com/xap/1.0/\x00" + minimal_xmp
    length = len(sig_and_packet) + 2
    jpeg = (
        b"\xff\xd8"
        + b"\xff\xd0"  # RST0 — standalone, no length
        + b"\xff\xe1"
        + length.to_bytes(2, "big")
        + sig_and_packet
        + b"\xff\xd9"
    )
    assert _find_xmp_packet_jpeg(jpeg) == minimal_xmp


def test_find_xmp_packet_jpeg_skips_padding_fill_bytes() -> None:
    """Some encoders emit ``0xFF`` fill bytes between segments. The walker
    advances past them by one byte at a time."""
    minimal_xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/" />'
    sig_and_packet = b"http://ns.adobe.com/xap/1.0/\x00" + minimal_xmp
    length = len(sig_and_packet) + 2
    jpeg = (
        b"\xff\xd8"
        + b"\xff\xff\xff"  # padding fill bytes
        + b"\xe1"
        + length.to_bytes(2, "big")
        + sig_and_packet
        + b"\xff\xd9"
    )
    assert _find_xmp_packet_jpeg(jpeg) == minimal_xmp


def test_find_xmp_packet_jpeg_stops_on_invalid_marker_prefix() -> None:
    """A byte where ``0xFF`` should be but isn't terminates the walk —
    malformed JPEG."""
    jpeg = b"\xff\xd8\x00\xe1\x00\x05hello"
    assert _find_xmp_packet_jpeg(jpeg) is None


def test_find_xmp_packet_jpeg_stops_at_sos() -> None:
    """``SOS`` introduces entropy-coded data — walker stops before reading
    past it. An XMP-shaped payload after SOS must not be returned."""
    jpeg = (
        b"\xff\xd8"
        + b"\xff\xda"  # SOS
        # Past this point the walker must stop, even if a fake XMP packet follows
        + b"\xff\xe1\x00\x05"
        + b"http://ns.adobe.com/xap/1.0/\x00"
    )
    assert _find_xmp_packet_jpeg(jpeg) is None


def test_find_xmp_packet_jpeg_stops_on_short_length_field() -> None:
    """A length field smaller than 2 (its own size) is malformed."""
    jpeg = b"\xff\xd8\xff\xe1\x00\x01\xff\xd9"
    assert _find_xmp_packet_jpeg(jpeg) is None


def test_find_xmp_packet_jpeg_ignores_non_xmp_app1() -> None:
    """An APP1 segment whose payload doesn't begin with the XMP signature
    is skipped (typical case: APP1 carrying EXIF). Walker continues to
    the next segment."""
    exif_payload = b"Exif\x00\x00fakeexif"
    length = len(exif_payload) + 2
    jpeg = b"\xff\xd8" + b"\xff\xe1" + length.to_bytes(2, "big") + exif_payload + b"\xff\xd9"
    assert _find_xmp_packet_jpeg(jpeg) is None


def test_find_xmp_packet_jpeg_truncated_length_field() -> None:
    """A segment marker followed by fewer than 2 bytes (the length field's
    own size) hits the ``offset + 4 > n`` guard. Without that guard, we'd
    read past end-of-buffer constructing the length integer."""
    jpeg = b"\xff\xd8\xff\xe1\x00"  # marker present, only 1 length byte
    assert _find_xmp_packet_jpeg(jpeg) is None


def test_find_xmp_packet_jpeg_loop_exhausts_without_xmp() -> None:
    """A buffer that walks through one or more non-XMP segments and ends
    cleanly at the buffer boundary (no SOS, no EOI) reaches the natural
    end-of-loop and returns None — exercises the post-loop fall-through
    rather than any of the early returns."""
    # SOI + APP0 (JFIF-shaped, non-XMP) consuming the rest of the buffer.
    jfif_payload = b"JFIF\x00fakeheader"
    length = len(jfif_payload) + 2
    # Build the buffer so that after processing APP0, offset advances exactly
    # to len(buffer) — the loop's `while offset + 1 < n` condition then exits
    # without re-entering, and we hit the post-loop `return None`.
    jpeg = b"\xff\xd8\xff\xe0" + length.to_bytes(2, "big") + jfif_payload
    assert _find_xmp_packet_jpeg(jpeg) is None


def test_find_xmp_packet_png_chunk_data_overruns_buffer() -> None:
    """A PNG chunk whose declared data length walks past end-of-buffer
    (with room for the chunk header but not the data) hits the
    ``data_end + 4 > n`` guard. Need a buffer at least 20 bytes so the
    loop's ``offset + 12 <= n`` precondition lets us into the chunk
    body — otherwise we'd never reach the inner guard."""
    overrun = (
        _PNG_SIGNATURE
        + (9999).to_bytes(4, "big")  # declared length far exceeds remaining bytes
        + b"abcd"  # any chunk type
        + b"xxxx"  # 4 dummy bytes — enough to satisfy the outer 20-byte threshold
    )
    assert _find_xmp_packet_png(overrun) == (None, [], [])


# ---------------------------------------------------------------------------
# Helper unit tests — _find_xmp_packet_png and _read_itxt_xmp
# ---------------------------------------------------------------------------


def _build_png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """Build a single PNG chunk (length + type + data + CRC). Test-side
    duplicate of the script-side helper so tests stay independent of
    ``scripts/``."""
    return (
        len(data).to_bytes(4, "big")
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data))
    )


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def test_find_xmp_packet_png_no_xmp_returns_none() -> None:
    """A PNG with no XMP iTXt chunk → ``(None, [], [])``."""
    assert _find_xmp_packet_png(_PNG_SIGNATURE) == (None, [], [])


def test_find_xmp_packet_png_handles_truncated_chunk() -> None:
    """A chunk header declaring more data than remains → ``(None, [], [])``, no crash."""
    truncated = _PNG_SIGNATURE + (9999).to_bytes(4, "big") + b"iTXt" + b"abc"
    assert _find_xmp_packet_png(truncated) == (None, [], [])


def test_find_xmp_packet_png_decompresses_compressed_xmp() -> None:
    """An iTXt chunk with compression-flag=1 (zlib) is decompressed via
    stdlib ``zlib`` and the inflated XMP packet returned. Real-world XMP-PNG
    files do exist with compressed iTXt; this exercises the success path."""
    real_xmp = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" />'
        b"</x:xmpmeta>"
    )
    compressed_xmp = zlib.compress(real_xmp)
    # iTXt: keyword \0 compression-flag(1) compression-method(0) lang \0 tkw \0 text
    payload = b"XML:com.adobe.xmp\x00\x01\x00\x00\x00" + compressed_xmp
    chunk = _build_png_chunk(b"iTXt", payload)
    png = _PNG_SIGNATURE + chunk
    packet, warnings, errors = _find_xmp_packet_png(png)
    assert packet == real_xmp
    assert warnings == []
    assert errors == []


def test_find_xmp_packet_png_errors_on_malformed_compressed_xmp() -> None:
    """A compression-flag=1 iTXt whose body isn't valid zlib triggers
    ``zlib.error``; the walker surfaces an error (not warning) instead of
    raising — keeps the parse no-crash invariant for adversarial inputs.
    Severity matches the malformed-XML path: "we found a chunk but
    couldn't decode it"."""
    payload = b"XML:com.adobe.xmp\x00\x01\x00\x00\x00not valid zlib data"
    chunk = _build_png_chunk(b"iTXt", payload)
    png = _PNG_SIGNATURE + chunk
    packet, warnings, errors = _find_xmp_packet_png(png)
    assert packet is None
    assert warnings == []
    assert any("Malformed compressed" in e for e in errors)


def test_find_xmp_packet_png_errors_on_unknown_compression_flag() -> None:
    """The PNG iTXt spec defines compression-flag values 0 (uncompressed)
    and 1 (zlib). Any other value indicates a malformed chunk; surface as
    an error rather than blindly reading the rest as text. Defensive
    against malformed inputs and parity with the malformed-zlib case."""
    malformed = b"XML:com.adobe.xmp\x00\x07\x00\x00\x00<looks like XMP body>"
    chunk = _build_png_chunk(b"iTXt", malformed)
    png = _PNG_SIGNATURE + chunk
    packet, warnings, errors = _find_xmp_packet_png(png)
    assert packet is None
    assert warnings == []
    assert any("Unrecognized iTXt compression flag" in e for e in errors)


def test_find_xmp_packet_png_errors_on_decompression_bomb() -> None:
    """Defends against the zlib-bomb DoS: a small compressed iTXt whose
    inflated payload would exceed ``_MAX_DECOMPRESSED_XMP_BYTES``. The
    walker rejects it via ``decompressobj``'s ``max_length`` cap and
    ``unconsumed_tail`` check — bomb is detected without allocating the
    full output. Surfaces as an error so the security gate is visible.

    The bomb payload is constructed by compressing a string longer than
    the cap; ``zlib.compress`` of repeated bytes has near-1000:1 ratio, so
    the compressed input is tiny."""
    bomb_payload = b"a" * (_MAX_DECOMPRESSED_XMP_BYTES + 1024)
    compressed = zlib.compress(bomb_payload)
    # Sanity: compressed size is way under the cap (typically <1 KB for ~16 MB
    # of repeated bytes), so the walker has to actually start decompressing
    # to encounter the cap — pinning the bomb-detection path.
    assert len(compressed) < 1024 * 1024  # well under 1 MB
    payload = b"XML:com.adobe.xmp\x00\x01\x00\x00\x00" + compressed
    chunk = _build_png_chunk(b"iTXt", payload)
    png = _PNG_SIGNATURE + chunk
    packet, warnings, errors = _find_xmp_packet_png(png)
    assert packet is None
    assert warnings == []
    assert any("decompression bomb" in e.lower() for e in errors)


def test_find_xmp_packet_png_skips_non_xmp_itxt() -> None:
    """An iTXt chunk whose keyword is not ``XML:com.adobe.xmp`` is skipped.
    Other iTXt usages (e.g. arbitrary user metadata) shouldn't be confused
    for XMP."""
    payload = b"Author\x00\x00\x00\x00\x00Alice"
    chunk = _build_png_chunk(b"iTXt", payload)
    png = _PNG_SIGNATURE + chunk
    assert _find_xmp_packet_png(png) == (None, [], [])


def test_read_itxt_xmp_handles_missing_keyword_terminator() -> None:
    """An iTXt payload with no NUL terminator after the keyword → ``(None, [], [])``."""
    assert _read_itxt_xmp(b"XML:com.adobe.xmp_no_nul_here") == (None, [], [])


def test_read_itxt_xmp_handles_truncated_after_keyword() -> None:
    """An iTXt payload that ends before the compression flags fits → ``(None, [], [])``."""
    assert _read_itxt_xmp(b"XML:com.adobe.xmp\x00") == (None, [], [])


def test_read_itxt_xmp_handles_missing_lang_terminator() -> None:
    """An iTXt payload missing the language-tag NUL terminator → ``(None, [], [])``."""
    assert _read_itxt_xmp(b"XML:com.adobe.xmp\x00\x00\x00xx-no-end") == (None, [], [])


def test_read_itxt_xmp_handles_missing_translated_keyword_terminator() -> None:
    """An iTXt payload missing the translated-keyword NUL terminator → ``(None, [], [])``."""
    assert _read_itxt_xmp(b"XML:com.adobe.xmp\x00\x00\x00\x00no-end") == (None, [], [])


# ---------------------------------------------------------------------------
# Helper unit tests — _has_extended_xmp
# ---------------------------------------------------------------------------


def test_has_extended_xmp_attribute_form() -> None:
    """The ``HasExtendedXMP`` marker is detected when present as an
    attribute on ``<rdf:Description>``."""
    xml = (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about=""'
        '  xmlns:xmpNote="http://ns.adobe.com/xmp/note/"'
        '  xmpNote:HasExtendedXMP="abc" />'
        "</rdf:RDF>"
    )
    root = fromstring(xml)
    assert _has_extended_xmp(root) is True


def test_has_extended_xmp_element_form() -> None:
    """The marker is also detected when present as a child element."""
    xml = (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about=""'
        '  xmlns:xmpNote="http://ns.adobe.com/xmp/note/">'
        "<xmpNote:HasExtendedXMP>abc</xmpNote:HasExtendedXMP>"
        "</rdf:Description>"
        "</rdf:RDF>"
    )
    root = fromstring(xml)
    assert _has_extended_xmp(root) is True


def test_has_extended_xmp_absent() -> None:
    """No marker → False, no false positives."""
    xml = (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" />'
        "</rdf:RDF>"
    )
    root = fromstring(xml)
    assert _has_extended_xmp(root) is False


def test_has_extended_xmp_no_rdf_root_returns_false() -> None:
    """A tree without an ``rdf:RDF`` element returns False — the marker
    can only live inside RDF. Defensive against malformed-but-parseable
    XMP-like trees."""
    xml = "<root><just-noise /></root>"
    root = fromstring(xml)
    assert _has_extended_xmp(root) is False


# ---------------------------------------------------------------------------
# Misc invariants
# ---------------------------------------------------------------------------


def test_file_too_large_raises_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Files past :data:`MAX_FILE_SIZE_BYTES` raise :class:`FileTooLargeError`
    before any read begins. Same boundary contract as
    :class:`FileInfoExtractor`."""
    import stat as stat_mod

    from pixel_probe.core.extractors import xmp as xmp_module
    from pixel_probe.exceptions import FileTooLargeError

    out = tmp_path / "small.jpg"
    out.write_bytes(b"\xff\xd8\xff\xd9")

    fake_size = xmp_module.MAX_FILE_SIZE_BYTES + 1
    real_stat = Path.stat

    class _StatStub:
        # st_mode must signal "regular file" — Python 3.11's Path.is_file()
        # reads st_mode and checks S_ISREG. Without this, is_file() raises
        # AttributeError on the missing attribute (3.14 changed the lookup
        # path so the attribute miss never surfaces there).
        st_mode = stat_mod.S_IFREG | 0o644
        st_size = fake_size
        st_mtime = 0.0

    def _fake_stat(self: Path, *, follow_symlinks: bool = True) -> _StatStub:
        if self == out:
            return _StatStub()
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", _fake_stat)

    with pytest.raises(FileTooLargeError):
        XmpExtractor().extract(out)
