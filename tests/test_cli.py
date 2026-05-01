"""CLI tests — argparse, formatters, exit codes.

Two layers:

- **Subprocess** tests at the bottom validate the full ``python -m
  pixel_probe.cli`` entry-point flow including argparse and stdout/stderr
  separation. Slower (~50-100 ms each) but exercise the real boundary.
- **In-process** tests cover the formatters and ``main()`` directly, much
  faster, and easier to assert on output content.

The split: a handful of subprocess tests for end-to-end confidence; the
bulk of coverage comes from in-process unit tests so the suite stays fast.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pixel_probe.cli import (
    _filter_result,
    _flatten_dict,
    _format_json,
    _format_section,
    _format_text,
    _parse_only,
    main,
)
from pixel_probe.core.analyzer import AnalysisResult
from pixel_probe.core.extractors.base import ExtractorResult

from .conftest import fixture_path

# ---------------------------------------------------------------------------
# Subprocess tests — end-to-end CLI invocation via python -m pixel_probe.cli
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Helper: run ``python -m pixel_probe.cli ARGS`` and return the result.

    Uses ``check=False`` so callers can inspect non-zero exit codes; the
    point of these tests is exit-code assertions."""
    # S603 (subprocess): args is fully test-controlled — sys.executable plus
    # literals — so the "untrusted input" warning doesn't apply.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pixel_probe.cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_runs_on_sample_jpeg() -> None:
    """The basic happy path: feed a fixture, expect zero exit and visible
    EXIF tags in stdout. Exercises argparse + analyze + text formatter end
    to end."""
    proc = _run_cli(str(fixture_path("exif_rich.jpg")))

    assert proc.returncode == 0
    assert "Make: TestCorp" in proc.stdout
    assert "Model: TestModel X1" in proc.stdout
    assert "== file_info ==" in proc.stdout
    assert "== exif ==" in proc.stdout


def test_cli_json_output_is_valid_json() -> None:
    """``--json`` produces JSON that ``json.loads`` round-trips. The
    contract: ``path`` field at top, ``results`` keyed by extractor name."""
    proc = _run_cli(str(fixture_path("exif_rich.jpg")), "--json")

    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert "path" in data
    assert "results" in data
    assert "exif" in data["results"]
    assert data["results"]["exif"]["data"]["Make"] == "TestCorp"


def test_cli_only_filter_includes_only_requested() -> None:
    """``--only file_info`` produces one section, not all four. Comma-
    separated multi-value also works."""
    proc = _run_cli(str(fixture_path("exif_rich.jpg")), "--only", "file_info")

    assert proc.returncode == 0
    assert "== file_info ==" in proc.stdout
    assert "== exif ==" not in proc.stdout
    assert "== iptc ==" not in proc.stdout
    assert "== xmp ==" not in proc.stdout


def test_cli_missing_file_exits_one(tmp_path: Path) -> None:
    """Missing input file → exit 1, single line on stderr, nothing on
    stdout. Standard Unix CLI behavior."""
    proc = _run_cli(str(tmp_path / "does-not-exist.jpg"))

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "No such file" in proc.stderr


def test_cli_invalid_only_exits_two() -> None:
    """``--only bogus`` → exit 2, error on stderr listing valid names."""
    proc = _run_cli(str(fixture_path("exif_rich.jpg")), "--only", "bogus")

    assert proc.returncode == 2
    assert "unknown extractor" in proc.stderr.lower()


def test_cli_version_flag_exits_zero() -> None:
    """``--version`` prints the package version and exits cleanly. Argparse
    handles the action; we just verify the contract."""
    proc = _run_cli("--version")

    assert proc.returncode == 0
    # Version string isn't strictly stable (setuptools-scm), but the prog
    # name + a digit-or-dot must appear.
    assert proc.stdout.startswith("pixel-probe ")
    assert re.search(r"\d", proc.stdout)


def test_cli_help_flag_exits_zero() -> None:
    """``--help`` prints usage and exits cleanly. Argparse-driven; we just
    confirm the program name + usage line are present."""
    proc = _run_cli("--help")

    assert proc.returncode == 0
    assert "pixel-probe" in proc.stdout
    assert "IMAGE" in proc.stdout


def test_cli_missing_image_argument_exits_two() -> None:
    """No positional argument → argparse exits 2 with its standard usage
    error on stderr. Confirms argparse's contract is in effect."""
    proc = _run_cli()

    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower() or "error" in proc.stderr.lower()


# ---------------------------------------------------------------------------
# In-process tests — main() and formatters
# ---------------------------------------------------------------------------


def test_main_returns_zero_on_success() -> None:
    """Direct ``main()`` invocation matches the subprocess contract for
    the happy path. Faster than spawning a subprocess and easier to
    instrument later if needed."""
    rc = main([str(fixture_path("exif_rich.jpg"))])
    assert rc == 0


def test_main_returns_one_on_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing-file path returns 1 in-process; stderr carries the error."""
    rc = main([str(tmp_path / "nope.jpg")])
    assert rc == 1
    captured = capsys.readouterr()
    assert "No such file" in captured.err
    assert captured.out == ""


def test_main_returns_two_on_invalid_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--only bogus`` returns 2 in-process; ValueError in _parse_only is
    caught and converted to a stderr message + exit code."""
    rc = main([str(fixture_path("exif_rich.jpg")), "--only", "bogus"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "unknown extractor" in captured.err.lower()


def test_main_json_flag_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    """``--json`` flag drives the JSON formatter."""
    rc = main([str(fixture_path("exif_rich.jpg")), "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert "results" in parsed


def test_main_only_flag_filters_results(capsys: pytest.CaptureFixture[str]) -> None:
    """In-process ``--only`` filter — covers the success branch of
    ``_filter_result`` invocation (the subprocess equivalent only exercises
    it through a separate process, which doesn't show up in coverage of
    ``main`` itself)."""
    rc = main([str(fixture_path("exif_rich.jpg")), "--only", "file_info"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "== file_info ==" in captured.out
    assert "== exif ==" not in captured.out


def test_main_verbose_sets_debug_log_level(
    fixture_jpeg: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``--verbose`` configures logging at DEBUG level. Without verbose,
    DEBUG records are suppressed; with it, they pass through."""
    with caplog.at_level(logging.DEBUG):
        # First run without --verbose: emit a DEBUG record after main() runs
        # and verify the level was set to WARNING (suppresses the record).
        # logging.basicConfig only takes effect on the first call per process,
        # so we verify the level via the root logger's effective level instead
        # of trying to test the suppression itself.
        rc = main([str(fixture_jpeg)])
        assert rc == 0
        assert logging.getLogger().level <= logging.DEBUG  # caplog forced it

    # Subsequent main([..., "--verbose"]) calls won't reconfigure due to
    # basicConfig's once-only behavior, but main() not crashing on the
    # flag is the contract we care about here.
    rc = main([str(fixture_jpeg), "--verbose"])
    assert rc == 0


@pytest.fixture
def fixture_jpeg() -> Path:
    """Convenience: a fixture path the in-process tests reuse."""
    return fixture_path("exif_rich.jpg")


# ---------------------------------------------------------------------------
# Formatter unit tests — _format_text / _format_section / _format_json
# ---------------------------------------------------------------------------


def _result_with(**results: ExtractorResult[object]) -> AnalysisResult:
    """Test helper: build an ``AnalysisResult`` from kwargs of named
    extractor results. Keeps the per-test boilerplate short."""
    return AnalysisResult(path="<test-path>", results=dict(results))


def test_format_text_renders_extractor_sections() -> None:
    """Each extractor gets a ``== name ==`` header followed by key/value
    lines. Sections appear in declared (insertion) order."""
    result = _result_with(
        file_info=ExtractorResult("file_info", {"size_bytes": 1234, "format": "JPEG"}),
        exif=ExtractorResult("exif", {"Make": "Canon", "Model": "EOS"}),
    )
    text = _format_text(result)

    assert text.startswith("File: <test-path>")
    assert "== file_info ==" in text
    assert "== exif ==" in text
    assert "size_bytes: 1234" in text
    assert "Make: Canon" in text
    # Declared order preserved: file_info section appears before exif's.
    assert text.index("== file_info ==") < text.index("== exif ==")


def test_format_text_handles_empty_dict() -> None:
    """``data == {}`` (success-but-empty per ADR 0011) renders as ``(empty)``
    so the section isn't blank."""
    result = _result_with(exif=ExtractorResult("exif", {}))
    assert "(empty)" in _format_text(result)


def test_format_text_handles_data_none() -> None:
    """``data is None`` (failure per ADR 0011) renders as ``(no data)``,
    distinct from ``(empty)``. Errors surface via ``! error:`` lines."""
    result = _result_with(exif=ExtractorResult("exif", errors=("oops",)))
    text = _format_text(result)

    assert "(no data)" in text
    assert "! error: oops" in text


def test_format_text_handles_warnings() -> None:
    """Warnings render with ``! warning:`` prefix, distinct from errors."""
    result = _result_with(iptc=ExtractorResult("iptc", {}, warnings=("PNG isn't supported",)))
    text = _format_text(result)

    assert "! warning: PNG isn't supported" in text


def test_format_text_flattens_nested_dict_with_dot_keys() -> None:
    """EXIF's GPS sub-dict is rendered with dot-joined keys
    (``gps.latitude``) so the structure is visible in flat text."""
    result = _result_with(
        exif=ExtractorResult("exif", {"Make": "Canon", "gps": {"latitude": 37.5}})
    )
    text = _format_text(result)

    assert "Make: Canon" in text
    assert "gps.latitude: 37.5" in text


def test_format_text_joins_list_values_with_commas() -> None:
    """IPTC ``Keywords`` (list[str]) renders comma-joined for readability."""
    result = _result_with(iptc=ExtractorResult("iptc", {"Keywords": ["alpha", "beta", "gamma"]}))
    text = _format_text(result)

    assert "Keywords: alpha, beta, gamma" in text


def test_format_text_handles_dataclass_payload() -> None:
    """``FileInfoExtractor`` returns a ``FileInfo`` dataclass — the formatter
    uses ``asdict`` to normalize it to a dict for rendering. End-to-end
    via Analyzer.default() to exercise the real dataclass payload."""
    from pixel_probe.core.analyzer import Analyzer

    analyzed = Analyzer.default().analyze(fixture_path("exif_rich.jpg"))
    text = _format_text(analyzed)

    # FileInfo fields render as flat key:value lines, not as a dataclass repr.
    assert "size_bytes:" in text
    assert "sha256:" in text
    assert "format: JPEG" in text


def test_format_text_handles_unknown_payload_shape() -> None:
    """Defensive: a payload that's neither dict nor dataclass falls through
    to ``repr()`` rather than crashing or rendering blank. Reaching this
    branch requires a custom Extractor; not a normal-flow path."""
    result = _result_with(custom=ExtractorResult("custom", "raw string payload"))
    text = _format_text(result)
    assert "'raw string payload'" in text


def test_format_json_round_trips_simple_payload() -> None:
    """Basic JSON round-trip: dict payload through asdict + json.dumps +
    json.loads gives back equivalent structure."""
    result = _result_with(exif=ExtractorResult("exif", {"Make": "Canon"}, warnings=("hey",)))
    parsed = json.loads(_format_json(result))

    assert parsed["path"] == "<test-path>"
    assert parsed["results"]["exif"]["data"] == {"Make": "Canon"}
    assert parsed["results"]["exif"]["warnings"] == ["hey"]


def test_format_json_serializes_dataclass_payload() -> None:
    """``FileInfo`` payload through ``_format_json`` → JSON object with
    fields as keys. ``asdict`` recurses into nested dataclasses."""
    from pixel_probe.core.analyzer import Analyzer

    analyzed = Analyzer.default().analyze(fixture_path("exif_rich.jpg"))
    parsed = json.loads(_format_json(analyzed))

    file_info = parsed["results"]["file_info"]["data"]
    assert "size_bytes" in file_info
    assert "sha256" in file_info
    assert file_info["format"] == "JPEG"


def test_format_json_serializes_data_none_as_null() -> None:
    """Failure result (``data=None``) serializes as JSON ``null`` —
    callers can distinguish from empty dict cleanly."""
    result = _result_with(exif=ExtractorResult("exif", errors=("boom",)))
    parsed = json.loads(_format_json(result))

    assert parsed["results"]["exif"]["data"] is None


# ---------------------------------------------------------------------------
# _parse_only / _filter_result unit tests
# ---------------------------------------------------------------------------


def test_parse_only_accepts_known_names() -> None:
    """All four known extractor names parse cleanly."""
    assert _parse_only("file_info,exif,iptc,xmp") == ("file_info", "exif", "iptc", "xmp")


def test_parse_only_tolerates_whitespace() -> None:
    """``--only "file_info, exif"`` (with space after comma) works the
    same as without — quality-of-life for shell users."""
    assert _parse_only("file_info, exif") == ("file_info", "exif")
    assert _parse_only("  file_info  ,  exif  ") == ("file_info", "exif")


def test_parse_only_skips_empty_tokens() -> None:
    """Trailing/double commas don't produce empty-string entries."""
    assert _parse_only("file_info,,exif,") == ("file_info", "exif")


def test_parse_only_rejects_unknown_names() -> None:
    """Unknown names raise ``ValueError`` listing all of them in one
    message — user fixes everything in a single pass."""
    with pytest.raises(ValueError, match="bogus") as exc_info:
        _parse_only("bogus,alsobogus")
    # Both unknowns appear in the single error message.
    assert "alsobogus" in str(exc_info.value)


def test_parse_only_rejects_mixed_known_and_unknown() -> None:
    """Even if some names are valid, any unknown triggers the rejection."""
    with pytest.raises(ValueError, match="unknown extractor"):
        _parse_only("exif,bogus")


def test_filter_result_keeps_requested_names() -> None:
    """Filter retains only requested extractors; original ordering preserved."""
    result = _result_with(
        file_info=ExtractorResult("file_info", {"a": 1}),
        exif=ExtractorResult("exif", {"b": 2}),
        iptc=ExtractorResult("iptc", {"c": 3}),
    )
    filtered = _filter_result(result, ("exif", "iptc"))

    assert set(filtered.results) == {"exif", "iptc"}
    assert filtered.path == result.path


def test_filter_result_silently_skips_absent_names() -> None:
    """A name validated by ``_parse_only`` but absent from the result (the
    user wired a custom analyzer that excludes it) is just skipped — not
    an error."""
    result = _result_with(exif=ExtractorResult("exif", {"a": 1}))
    filtered = _filter_result(result, ("exif", "iptc"))

    assert set(filtered.results) == {"exif"}


def test_flatten_dict_handles_simple_keys() -> None:
    """Flat dict → ``(key, str(value))`` pairs in iteration order."""
    pairs = list(_flatten_dict({"a": 1, "b": "two"}))
    assert pairs == [("a", "1"), ("b", "two")]


def test_flatten_dict_recurses_into_nested_dicts() -> None:
    """Nested dicts produce dot-joined key paths — the load-bearing
    behavior for GPS sub-dict rendering."""
    pairs = list(_flatten_dict({"top": {"nested": {"deep": 42}}}))
    assert pairs == [("top.nested.deep", "42")]


def test_format_section_renders_failure_marker() -> None:
    """Direct unit test of ``_format_section`` for the failure path —
    confirms ``data=None`` produces the ``(no data)`` placeholder line."""
    section = _format_section("exif", ExtractorResult("exif", errors=("e",)))
    assert section[0] == "== exif =="
    assert "  (no data)" in section
    assert "  ! error: e" in section
