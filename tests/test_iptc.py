"""IPTC extractor tests — integration, helper unit tests, error isolation, charset handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from pixel_probe.core.analyzer import Analyzer
from pixel_probe.core.extractors.iptc import (
    IPTC_TAGS,
    IptcExtractor,
    _decode,
    _iter_iim_records,
    _iter_irbs,
    _iter_segments,
    _resolve_charset,
)
from pixel_probe.exceptions import MissingFileError

from .conftest import fixture_path

# ---------------------------------------------------------------------------
# Integration: real fixtures
# ---------------------------------------------------------------------------


def test_parses_basic_text_fields() -> None:
    """Title / Byline / Copyright extracted as scalar strings from the rich
    fixture. Non-ASCII (the © in Copyright) round-trips through UTF-8."""
    result = IptcExtractor().extract(fixture_path("iptc_basic.jpg"))

    assert result.extractor_name == "iptc"
    assert result.errors == ()
    assert result.warnings == ()
    assert result.has_data
    assert result.data["Title"] == "Sample IPTC Title"
    assert result.data["Byline"] == "Test Author"
    assert result.data["Copyright"] == "© 2026 TestCorp"


def test_keywords_repeatable_as_list() -> None:
    """The IIM ``Keywords`` dataset is repeatable — three records in the
    fixture must accumulate into ``list[str]`` in declared order. This is
    the load-bearing repeatable-tag contract."""
    result = IptcExtractor().extract(fixture_path("iptc_basic.jpg"))

    assert result.data["Keywords"] == ["alpha", "beta", "gamma"]


def test_charset_record_not_surfaced_in_data() -> None:
    """``CodedCharacterSet`` (1, 90) is consumed for charset selection; it
    must not leak into the public payload — that's an implementation detail."""
    result = IptcExtractor().extract(fixture_path("iptc_basic.jpg"))

    assert "CodedCharacterSet" not in result.data
    # Defensive: also verify the raw escape isn't smuggled in under any name.
    assert all(v != b"\x1b%G" for v in result.data.values() if isinstance(v, bytes))


def test_returns_empty_for_image_without_iptc() -> None:
    """No APP13 block is normal — return empty data, no errors, no warnings."""
    result = IptcExtractor().extract(fixture_path("iptc_none.jpg"))

    assert result.data == {}
    assert result.errors == ()
    assert result.warnings == ()
    assert not result.has_data


def test_handles_corrupt_block() -> None:
    """A malformed IIM record (declared size 9999 with only ~9 bytes of
    value) must be detected via bounds-checking; the walk stops cleanly
    and no exception propagates. Whatever was parsed before the corruption
    ships in data — for this fixture, that's nothing (the corrupt record
    is the only one)."""
    result = IptcExtractor().extract(fixture_path("iptc_corrupt.jpg"))

    assert result.data == {}
    assert result.errors == ()
    assert result.warnings == ()


def test_non_jpeg_returns_error() -> None:
    """PNG → IPTC v0.1 returns ``data=None`` + one error per ADR 0011.
    IPTC produces zero data on non-JPEG input — that's a failure, not a
    warning. The error string makes the unsupported-format reason
    visible to consumers."""
    result = IptcExtractor().extract(fixture_path("tiny.png"))

    assert result.data is None
    assert result.warnings == ()
    assert result.errors == ("File is not a JPEG; IPTC v0.1 supports JPEG only",)


def test_text_file_returns_error() -> None:
    """Same contract for non-image files — caller chose to point us at a
    non-JPEG, IPTC produces zero data, so per ADR 0011: ``data=None`` +
    error (not warning)."""
    result = IptcExtractor().extract(fixture_path("not_an_image.txt"))

    assert result.data is None
    assert result.errors == ("File is not a JPEG; IPTC v0.1 supports JPEG only",)


def test_missing_file_raises_typed_error(tmp_path: Path) -> None:
    """Missing input is a :class:`MissingFileError` (a
    :class:`PixelProbeError` subclass) — the boundary contract that
    every extractor honours."""
    with pytest.raises(MissingFileError):
        IptcExtractor().extract(tmp_path / "does-not-exist.jpg")


def test_idempotent() -> None:
    """Same input → same output, every call. Pure I/O, no global state."""
    extractor = IptcExtractor()
    a = extractor.extract(fixture_path("iptc_basic.jpg"))
    b = extractor.extract(fixture_path("iptc_basic.jpg"))
    assert a.data == b.data
    assert a.errors == b.errors


def test_wired_into_default_analyzer() -> None:
    """The default factory must include IPTC alongside file_info and EXIF —
    end-to-end smoke that the extractor is reachable through the public API."""
    result = Analyzer.default().analyze(fixture_path("iptc_basic.jpg"))

    assert "iptc" in result.results
    assert result.results["iptc"].has_data
    assert result.results["iptc"].data["Title"] == "Sample IPTC Title"


# ---------------------------------------------------------------------------
# Charset handling
# ---------------------------------------------------------------------------


def test_resolve_charset_known_utf8() -> None:
    """The ISO 2022 escape ``\\x1b%G`` selects UTF-8, no warning."""
    codec, warnings = _resolve_charset(b"\x1b%G")
    assert codec == "utf-8"
    assert warnings == []


def test_resolve_charset_none_default() -> None:
    """Absent ``CodedCharacterSet`` defaults to UTF-8 silently — that's the
    most common case in modern files."""
    codec, warnings = _resolve_charset(None)
    assert codec == "utf-8"
    assert warnings == []


def test_resolve_charset_unknown_warns() -> None:
    """An unrecognised escape produces one warning so the caller can
    surface it. We still default to UTF-8 (rather than raising) to keep
    the extractor's no-crash contract."""
    codec, warnings = _resolve_charset(b"\x1b%5")  # an arbitrary unknown escape
    assert codec == "utf-8"
    assert len(warnings) == 1
    assert "Unrecognized" in warnings[0]


def test_unrecognized_charset_in_real_fixture_warns(tmp_path: Path) -> None:
    """End-to-end: a fixture with a non-UTF-8 charset escape produces a
    warning on the result envelope. Built inline (no on-disk fixture)
    because the only thing the test needs is a JPEG with a CodedCharacterSet
    record carrying a non-UTF-8 value."""
    out = tmp_path / "bogus_charset.jpg"
    out.write_bytes(
        _build_jpeg_with_iim(
            [
                (1, 90, b"\x1b%5"),  # bogus charset escape
                (2, 5, b"Title"),
            ]
        )
    )
    result = IptcExtractor().extract(out)

    assert result.data == {"Title": "Title"}
    assert any("Unrecognized" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Helper unit tests — _decode
# ---------------------------------------------------------------------------


def test_decode_strips_trailing_nul() -> None:
    """Trailing NULs (sometimes added by encoders) are removed; embedded
    NULs are preserved as-is."""
    assert _decode(b"hello\x00", "utf-8") == "hello"
    assert _decode(b"hi\x00there", "utf-8") == "hi\x00there"


def test_decode_uses_replace_on_bad_bytes() -> None:
    """Malformed bytes don't raise — they become the Unicode replacement
    character. A single bad byte must not kill an entire IPTC parse."""
    decoded = _decode(b"\xff\xfe\xfd", "utf-8")
    assert isinstance(decoded, str)
    # At least one replacement character (U+FFFD) was produced.
    assert "�" in decoded


def test_decode_falls_back_on_unknown_codec() -> None:
    """An unrecognised codec name doesn't raise — falls back to UTF-8 with
    replacement. Today ``_resolve_charset`` only ever returns ``"utf-8"``,
    so this branch is unreachable from normal flow; the guard exists so a
    future extension to the charset map can't crash the parser. Defensive
    tests for unreachable-today branches catch regressions early if the
    invariant changes."""
    assert _decode(b"hello", "definitely-not-a-real-codec") == "hello"


# ---------------------------------------------------------------------------
# Helper unit tests — _iter_segments
# ---------------------------------------------------------------------------


def test_iter_segments_returns_on_non_jpeg() -> None:
    """A buffer not starting with SOI yields nothing — the iterator never
    raises, callers don't need to pre-validate for safety (they may still
    want to for warning purposes)."""
    assert list(_iter_segments(b"not a jpeg")) == []
    assert list(_iter_segments(b"")) == []


def test_iter_segments_yields_app13_payload() -> None:
    """A minimal SOI + APP13 + EOI buffer yields the APP13 payload."""
    payload = b"hello"
    length = len(payload) + 2
    jpeg = b"\xff\xd8" + b"\xff\xed" + length.to_bytes(2, "big") + payload + b"\xff\xd9"

    segments = list(_iter_segments(jpeg))
    assert segments == [(0xED, b"hello")]


def test_iter_segments_handles_truncated_length() -> None:
    """A segment whose declared length walks past end-of-buffer terminates
    the walk cleanly — no IndexError, no infinite loop."""
    # SOI + APP13 marker + length=999 (fake) + 3 bytes of payload (not 999).
    jpeg = b"\xff\xd8\xff\xed" + (999).to_bytes(2, "big") + b"abc"
    assert list(_iter_segments(jpeg)) == []


def test_iter_segments_handles_length_below_two() -> None:
    """A length field below 2 (the minimum, since length includes itself)
    is malformed; the walker stops without yielding."""
    jpeg = b"\xff\xd8\xff\xed\x00\x01\xff\xd9"
    assert list(_iter_segments(jpeg)) == []


def test_iter_segments_skips_padding_bytes() -> None:
    """Some encoders emit ``0xFF`` fill bytes between segments. The walker
    advances past them by one byte at a time until a real marker shows up."""
    payload = b"x"
    length = len(payload) + 2
    jpeg = (
        b"\xff\xd8"
        + b"\xff\xff\xff"  # three fill bytes before APP13
        + b"\xed"
        + length.to_bytes(2, "big")
        + payload
        + b"\xff\xd9"
    )
    segments = list(_iter_segments(jpeg))
    assert segments == [(0xED, b"x")]


def test_iter_segments_stops_at_sos() -> None:
    """``SOS`` introduces entropy-coded image data — segments past it are
    not real segments. The walker must stop before yielding past SOS."""
    payload = b"y"
    length = len(payload) + 2
    jpeg = (
        b"\xff\xd8"
        + b"\xff\xed"
        + length.to_bytes(2, "big")
        + payload
        + b"\xff\xda"
        + b"garbage that would otherwise be misparsed"
    )
    assert list(_iter_segments(jpeg)) == [(0xED, b"y")]


def test_iter_segments_skips_standalone_markers() -> None:
    """``RST0..RST7`` are restart markers with no length payload. The walker
    advances past them without yielding."""
    payload = b"z"
    length = len(payload) + 2
    jpeg = (
        b"\xff\xd8"
        + b"\xff\xd0"  # RST0 — standalone, no length
        + b"\xff\xed"
        + length.to_bytes(2, "big")
        + payload
        + b"\xff\xd9"
    )
    segments = list(_iter_segments(jpeg))
    assert segments == [(0xED, b"z")]


def test_iter_segments_stops_on_non_ff_byte() -> None:
    """A byte that should be ``0xFF`` (segment marker prefix) but isn't
    indicates a malformed stream — the walker stops cleanly."""
    jpeg = b"\xff\xd8\x00\xed\x00\x05hello"
    assert list(_iter_segments(jpeg)) == []


def test_iter_segments_stops_when_length_field_truncated() -> None:
    """A segment marker followed by fewer than 2 bytes (the length field
    requires 2) terminates the walk — covers the ``offset + 4 > n`` guard
    that protects against truncated headers."""
    jpeg = b"\xff\xd8\xff\xed\x00"  # marker present, only 1 length byte
    assert list(_iter_segments(jpeg)) == []


# ---------------------------------------------------------------------------
# Helper unit tests — _iter_irbs
# ---------------------------------------------------------------------------


def test_iter_irbs_walks_basic_block() -> None:
    """A single 8BIM IRB with resource ID 0x0404 and a 4-byte data block."""
    data = b"abcd"
    irb = b"8BIM" + (0x0404).to_bytes(2, "big") + b"\x00\x00" + len(data).to_bytes(4, "big") + data
    blocks = list(_iter_irbs(irb))
    assert blocks == [(0x0404, b"abcd")]


def test_iter_irbs_pads_odd_data_to_even() -> None:
    """If a data block is odd-length, the next IRB starts after a 1-byte
    pad. Two consecutive IRBs with odd-length data exercise this."""
    irb1_data = b"abc"  # odd
    irb1 = (
        b"8BIM"
        + (0x0404).to_bytes(2, "big")
        + b"\x00\x00"
        + len(irb1_data).to_bytes(4, "big")
        + irb1_data
        + b"\x00"  # pad to even
    )
    irb2_data = b"de"  # even
    irb2 = (
        b"8BIM"
        + (0x040C).to_bytes(2, "big")
        + b"\x00\x00"
        + len(irb2_data).to_bytes(4, "big")
        + irb2_data
    )
    blocks = list(_iter_irbs(irb1 + irb2))
    assert blocks == [(0x0404, b"abc"), (0x040C, b"de")]


def test_iter_irbs_stops_on_bad_signature() -> None:
    """An IRB-shaped block whose first 4 bytes aren't ``8BIM`` terminates
    the walk — never raise, never read out of bounds."""
    bad = b"BADSIG\x04\x04\x00\x00\x00\x00\x00\x00"
    assert list(_iter_irbs(bad)) == []


def test_iter_irbs_stops_on_truncated_size() -> None:
    """An IRB whose data-size field is missing/short terminates cleanly."""
    short = b"8BIM" + (0x0404).to_bytes(2, "big") + b"\x00\x00\x00\x00"  # missing 1 size byte
    assert list(_iter_irbs(short)) == []


def test_iter_irbs_stops_on_oversized_data() -> None:
    """Declared data size larger than remaining buffer → clean stop."""
    truncated = (
        b"8BIM"
        + (0x0404).to_bytes(2, "big")
        + b"\x00\x00"
        + (9999).to_bytes(4, "big")
        + b"only-a-few-bytes"
    )
    assert list(_iter_irbs(truncated)) == []


def test_iter_irbs_handles_named_irb() -> None:
    """A named IRB (Pascal name with non-zero length) is parsed correctly —
    the name bytes plus pad-to-even are skipped before the size field."""
    name = b"name"  # 4 bytes; name_total = 1 + 4 = 5, padded to 6
    data = b"xy"
    irb = (
        b"8BIM"
        + (0x0404).to_bytes(2, "big")
        + bytes([len(name)])
        + name
        + b"\x00"  # pad to even
        + len(data).to_bytes(4, "big")
        + data
    )
    assert list(_iter_irbs(irb)) == [(0x0404, b"xy")]


def test_iter_irbs_handles_odd_name_length_no_padding() -> None:
    """When the Pascal name length is odd, ``name_total = 1 + name_len`` is
    even and the +=1 padding branch is *not* taken. Covers the false side
    of the ``if name_total % 2`` branch."""
    name = b"abc"  # 3 bytes; name_total = 1 + 3 = 4, even, no padding needed
    data = b"qr"
    irb = (
        b"8BIM"
        + (0x0404).to_bytes(2, "big")
        + bytes([len(name)])
        + name  # no trailing pad byte — total is already even
        + len(data).to_bytes(4, "big")
        + data
    )
    assert list(_iter_irbs(irb)) == [(0x0404, b"qr")]


def test_iter_irbs_stops_when_size_field_runs_off_end() -> None:
    """An IRB whose size field would lie past end-of-buffer terminates
    cleanly — covers the ``size_offset + 4 > n`` guard. We construct an
    IRB that *passes* the outer 12-byte length precondition but whose
    Pascal name expands past the available bytes (long name, no size
    field follows)."""
    # 12 bytes total: signature (4) + id (2) + name_len byte (1) +
    # 5 bytes of name. After the name we'd need 4 bytes for size, but
    # only 0 bytes remain.
    payload = b"8BIM" + (0x0404).to_bytes(2, "big") + bytes([5]) + b"abcde"
    assert len(payload) == 12  # passes the outer precondition
    assert list(_iter_irbs(payload)) == []


# ---------------------------------------------------------------------------
# Helper unit tests — _iter_iim_records
# ---------------------------------------------------------------------------


def test_iter_iim_records_walks_records() -> None:
    """Two well-formed records back-to-back yield in order."""
    block = (
        bytes([0x1C, 0x02, 0x05])
        + (5).to_bytes(2, "big")
        + b"hello"
        + bytes([0x1C, 0x02, 0x19])
        + (3).to_bytes(2, "big")
        + b"key"
    )
    records = list(_iter_iim_records(block))
    assert records == [(2, 5, b"hello"), (2, 0x19, b"key")]


def test_iter_iim_records_stops_on_extended_size() -> None:
    """High bit set in the size field marks an extended record (length-of-length
    semantics). v1 doesn't support these and can't safely advance past one,
    so the walker stops."""
    block = bytes([0x1C, 0x02, 0x05]) + (0x8005).to_bytes(2, "big") + b"abcde"
    assert list(_iter_iim_records(block)) == []


def test_iter_iim_records_stops_on_bad_marker() -> None:
    """Anything other than 0x1C in the tag-marker position terminates
    the walk — not a real IIM record."""
    block = bytes([0xAA, 0x02, 0x05]) + (1).to_bytes(2, "big") + b"x"
    assert list(_iter_iim_records(block)) == []


def test_iter_iim_records_stops_on_truncated_value() -> None:
    """Declared size walking past end-of-buffer terminates cleanly."""
    block = bytes([0x1C, 0x02, 0x05]) + (9999).to_bytes(2, "big") + b"truncated"
    assert list(_iter_iim_records(block)) == []


def test_iter_iim_records_handles_zero_length_value() -> None:
    """A zero-length value is structurally valid (some IIM datasets are
    flag-only) — yields the empty bytes."""
    block = bytes([0x1C, 0x02, 0x05, 0x00, 0x00])
    assert list(_iter_iim_records(block)) == [(2, 5, b"")]


# ---------------------------------------------------------------------------
# Misc invariants
# ---------------------------------------------------------------------------


def test_unknown_iptc_tags_silently_dropped(tmp_path: Path) -> None:
    """Datasets not in :data:`IPTC_TAGS` are silently dropped from the
    result — there are hundreds of obscure datasets in the IIM spec and
    surfacing all of them as ``Tag_X_Y`` would be noise. No warning,
    no error: the tag was simply ignored."""
    out = tmp_path / "unknown.jpg"
    out.write_bytes(
        _build_jpeg_with_iim(
            [
                (1, 90, b"\x1b%G"),
                (2, 5, b"Real Title"),
                (2, 200, b"obscure dataset not in our table"),  # not in IPTC_TAGS
            ]
        )
    )
    result = IptcExtractor().extract(out)

    assert result.data == {"Title": "Real Title"}
    assert result.warnings == ()
    assert result.errors == ()


def test_iptc_tag_table_no_overlapping_keys() -> None:
    """Sanity: every (record, dataset) pair in the tag table is unique —
    duplicates would silently overwrite. Trivially true for a hand-written
    dict, but a regression guard if the table grows."""
    keys = list(IPTC_TAGS.keys())
    assert len(keys) == len(set(keys))


def test_repeatable_and_scalar_tags_have_disjoint_names() -> None:
    """If a friendly name appears under both a repeatable and a non-repeatable
    ``(record, dataset)`` key, the dispatch in ``extract`` would crash:
    ``data.setdefault(name, [])`` returns the existing scalar string, and
    ``.append()`` on a ``str`` raises ``AttributeError``. Today only
    ``Keywords`` is repeatable, so this is true by construction — the test
    guards against drift if more datasets get added to either table."""
    from pixel_probe.core.extractors.iptc import _REPEATABLE_TAGS

    repeatable_names = {IPTC_TAGS[k] for k in _REPEATABLE_TAGS if k in IPTC_TAGS}
    scalar_names = {IPTC_TAGS[k] for k in IPTC_TAGS if k not in _REPEATABLE_TAGS}
    assert repeatable_names.isdisjoint(scalar_names), (
        f"Friendly tag names overlap between repeatable and scalar groups: "
        f"{repeatable_names & scalar_names}"
    )


def test_no_charset_record_uses_default_utf8(tmp_path: Path) -> None:
    """An IPTC block without a CodedCharacterSet record falls through the
    pre-pass without breaking and decodes with the UTF-8 default. Exercises
    the loop path where ``(rec, ds) == _CHARSET_RECORD`` is never true."""
    out = tmp_path / "no_charset.jpg"
    out.write_bytes(_build_jpeg_with_iim([(2, 5, b"Title Without Charset")]))
    result = IptcExtractor().extract(out)

    assert result.data == {"Title": "Title Without Charset"}
    assert result.warnings == ()


def test_app13_present_but_no_iptc_irb_returns_empty(tmp_path: Path) -> None:
    """A JPEG with an APP13/Photoshop block whose only IRB is *not* the
    IPTC resource (e.g. resource 0x040C, JPEG quality preset) returns
    empty data and no warning — the function-level early-exit on
    ``_find_iim_block(...) is None``."""
    # Build an APP13 with a single non-IPTC IRB (resource_id 0x040C, "JPEG quality").
    other_data = b"not iptc bytes"
    padded = other_data + (b"\x00" if len(other_data) % 2 else b"")
    irb = (
        b"8BIM"
        + (0x040C).to_bytes(2, "big")
        + b"\x00\x00"
        + len(other_data).to_bytes(4, "big")
        + padded
    )
    payload = b"Photoshop 3.0\x00" + irb
    length = len(payload) + 2
    app13 = b"\xff\xed" + length.to_bytes(2, "big") + payload
    out = tmp_path / "non_iptc_irb.jpg"
    out.write_bytes(b"\xff\xd8" + app13 + b"\xff\xd9")

    result = IptcExtractor().extract(out)
    assert result.data == {}
    assert result.warnings == ()
    assert result.errors == ()


def test_file_too_large_raises_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Files past :data:`MAX_FILE_SIZE_BYTES` raise :class:`FileTooLargeError`
    before any read begins. Same boundary contract as
    :class:`FileInfoExtractor` (we share the constant) — the orchestrator
    converts the raise into an error-only result downstream."""
    import stat as stat_mod

    from pixel_probe.core.extractors import iptc as iptc_module
    from pixel_probe.exceptions import FileTooLargeError

    out = tmp_path / "small.jpg"
    out.write_bytes(_build_jpeg_with_iim([(2, 5, b"x")]))

    fake_size = iptc_module.MAX_FILE_SIZE_BYTES + 1
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
        IptcExtractor().extract(out)


# ---------------------------------------------------------------------------
# Inline fixture builder — used by tests that need a synthetic JPEG
# ---------------------------------------------------------------------------
#
# Mirrors the helpers in ``scripts/build_fixtures.py`` but kept inline
# rather than imported because tests aren't a package consumer of the
# scripts/ tree (no ``scripts/__init__.py``, deliberately). Cheap to
# duplicate for a few-line builder.


def _build_jpeg_with_iim(records: list[tuple[int, int, bytes]]) -> bytes:
    """Build the minimum-viable JPEG byte stream with an APP13 IPTC IIM
    block carrying the given records. Used by tests that need a synthetic
    fixture without writing a script-level builder."""
    iim_block = b"".join(bytes([0x1C, r, d]) + len(v).to_bytes(2, "big") + v for r, d, v in records)
    padded = iim_block + (b"\x00" if len(iim_block) % 2 else b"")
    irb = (
        b"8BIM"
        + (0x0404).to_bytes(2, "big")
        + b"\x00\x00"
        + len(iim_block).to_bytes(4, "big")
        + padded
    )
    payload = b"Photoshop 3.0\x00" + irb
    length = len(payload) + 2
    app13 = b"\xff\xed" + length.to_bytes(2, "big") + payload
    # Minimum-viable JPEG: SOI + APP13 + EOI. Real decoders will reject this
    # as having no image data, but our parser only walks segments — it
    # doesn't decode pixels.
    return b"\xff\xd8" + app13 + b"\xff\xd9"
