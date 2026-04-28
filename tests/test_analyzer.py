"""Orchestrator tests — DI, ordering, exception conversion."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pixel_probe.core.analyzer import AnalysisResult, Analyzer
from pixel_probe.core.extractors.base import Extractor, ExtractorResult
from pixel_probe.core.extractors.file_info import FileInfoExtractor
from pixel_probe.exceptions import PixelProbeError

from .conftest import fixture_path


class _OkExtractor(Extractor[dict[str, Any]]):
    """Test double — returns a fixed payload."""

    payload_type = dict

    def __init__(self, name: str, payload: dict[str, Any] | None = None) -> None:
        self.name = name
        self._payload = payload if payload is not None else {"ok": True}

    def extract(self, path: Path) -> ExtractorResult[dict[str, Any]]:
        return ExtractorResult(self.name, self._payload)


class _RaisingExtractor(Extractor[dict[str, Any]]):
    """Test double — raises on extract.

    Accepts ``BaseException`` (not just ``Exception``) so we can verify the
    orchestrator's catch-all stops at ``Exception`` and lets ``KeyboardInterrupt``
    propagate.
    """

    payload_type = dict

    def __init__(self, name: str, exc: BaseException) -> None:
        self.name = name
        self._exc = exc

    def extract(self, path: Path) -> ExtractorResult[dict[str, Any]]:
        raise self._exc


def test_default_runs_file_info() -> None:
    """The default factory wires up the built-in extractor list."""
    analyzer = Analyzer.default()
    result = analyzer.analyze(fixture_path("tiny.jpg"))

    assert isinstance(result, AnalysisResult)
    assert "file_info" in result.results
    assert result.results["file_info"].has_data


def test_constructor_di_uses_provided_extractors() -> None:
    """Custom extractor lists are honored end-to-end — the extension story."""
    analyzer = Analyzer([_OkExtractor("alpha"), _OkExtractor("beta")])
    result = analyzer.analyze(fixture_path("tiny.jpg"))

    assert set(result.results) == {"alpha", "beta"}
    assert result.results["alpha"].data == {"ok": True}
    assert result.results["beta"].data == {"ok": True}


def test_preserves_declared_order() -> None:
    """Result-dict insertion order must match the order extractors were
    declared — downstream renderers (CLI, GUI tree) rely on this for
    consistent display.

    Python dicts preserve insertion order since 3.7; this asserts our
    code doesn't break that property."""
    analyzer = Analyzer([_OkExtractor("c"), _OkExtractor("a"), _OkExtractor("b")])
    result = analyzer.analyze(fixture_path("tiny.jpg"))

    assert list(result.results) == ["c", "a", "b"]


def test_exception_in_one_extractor_does_not_kill_others() -> None:
    """One bad parser must not break the rest — the orchestrator catches
    every ``Exception`` and converts it to an error-only result."""
    analyzer = Analyzer(
        [
            _OkExtractor("first"),
            _RaisingExtractor("middle", PixelProbeError("planned failure")),
            _OkExtractor("last"),
        ]
    )
    result = analyzer.analyze(fixture_path("tiny.jpg"))

    assert result.results["first"].has_data
    assert result.results["middle"].data is None
    assert result.results["middle"].errors == ("PixelProbeError: planned failure",)
    assert result.results["last"].has_data


def test_exception_message_includes_type_name() -> None:
    """Error messages preserve the exception class — that's part of the
    debuggability contract for whoever reads the result downstream."""
    analyzer = Analyzer([_RaisingExtractor("boom", FileNotFoundError("missing.jpg"))])
    result = analyzer.analyze(fixture_path("tiny.jpg"))

    msg = result.results["boom"].errors[0]
    assert msg.startswith("FileNotFoundError:")
    assert "missing.jpg" in msg


def test_keyboard_interrupt_propagates() -> None:
    """``KeyboardInterrupt`` and ``SystemExit`` aren't ``Exception`` subclasses
    in modern Python, so the orchestrator's catch-all ``except Exception``
    correctly lets them through."""
    analyzer = Analyzer([_RaisingExtractor("abort", KeyboardInterrupt())])

    with pytest.raises(KeyboardInterrupt):
        analyzer.analyze(fixture_path("tiny.jpg"))


def test_empty_extractor_list_returns_empty_result() -> None:
    """Edge case — analyzer with no extractors. Should return an empty
    result, not crash."""
    result = Analyzer([]).analyze(fixture_path("tiny.jpg"))

    assert result.results == {}
    assert result.path == str(fixture_path("tiny.jpg"))


def test_constructor_makes_defensive_copy() -> None:
    """Mutating the caller's list after construction must not affect the
    analyzer's view of its extractors — encapsulation invariant."""
    extractors: list[Extractor[Any]] = [_OkExtractor("alpha")]
    analyzer = Analyzer(extractors)

    extractors.append(_OkExtractor("beta"))  # caller mutates their reference

    result = analyzer.analyze(fixture_path("tiny.jpg"))
    assert list(result.results) == ["alpha"]


def test_default_returns_fresh_instances() -> None:
    """Each ``default()`` call must give an independent analyzer — no shared
    state between callers."""
    a, b = Analyzer.default(), Analyzer.default()
    assert a is not b
    # Private access intentional: testing the encapsulation contract.
    assert a._extractors is not b._extractors


def test_orchestrator_uses_real_file_info_end_to_end() -> None:
    """Integration: the real extractor + the real orchestrator + a real file."""
    analyzer = Analyzer([FileInfoExtractor()])
    result = analyzer.analyze(fixture_path("tiny.png"))

    assert result.results["file_info"].has_data
    info = result.results["file_info"].data
    assert info is not None
    assert info.format == "PNG"
