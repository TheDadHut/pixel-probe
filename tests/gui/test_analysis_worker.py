"""Tests for :class:`AnalysisWorker`.

The worker is single-method (``run``) but signal-based — tests exercise
the signal wiring synchronously by calling ``run()`` directly. The full
``QThread`` + ``moveToThread`` integration lives in Phase 5b's
``MainWindow`` test (where it's the load-bearing flow).

Two paths covered:

- Happy: ``analyzer.analyze`` returns; worker emits ``finished`` with
  the :class:`AnalysisResult`.
- Defensive: ``analyzer.analyze`` raises (rare — analyzer catches
  internally per ADR 0011); worker emits ``failed`` with a formatted
  error string.

``pytest-qt``'s ``qtbot.waitSignal`` would be the canonical way to
assert signals; for synchronous ``run()`` calls a captured-list
fixture is simpler and avoids spinning the event loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pixel_probe.core.analyzer import AnalysisResult, Analyzer
from pixel_probe.core.extractors.base import Extractor, ExtractorResult
from pixel_probe.gui.workers.analysis_worker import AnalysisWorker

from ..conftest import fixture_path

# No ``gui`` marker — synchronous ``run()`` calls don't need an event loop.
# Reserved for the ``QThread`` integration test in Phase 5b's MainWindow.


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RaisingAnalyzer:
    """Test double that mimics :class:`Analyzer` but raises on ``analyze``.

    Used to exercise the worker's defensive ``except Exception`` path. In
    normal flow this never fires (per ADR 0011, ``Analyzer.analyze``
    catches both ``PixelProbeError`` and any unexpected ``Exception``
    internally); the path exists for genuine infrastructure failures."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def analyze(self, path: Path) -> AnalysisResult:
        del path
        raise self._exc


class _OkExtractor(Extractor[dict[str, Any]]):
    """Test double — returns a fixed payload. Used to drive the
    worker's happy path without depending on the file_info / EXIF /
    IPTC / XMP extractors (which would pull a fixture file in)."""

    name = "ok"
    payload_type = dict

    def __init__(self) -> None:
        pass

    def extract(self, path: Path) -> ExtractorResult[dict[str, Any]]:
        del path
        return ExtractorResult(self.name, {"hello": "world"})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_emits_finished_with_analysis_result() -> None:
    """The worker's ``run()`` slot calls ``Analyzer.analyze`` and emits
    the result on the ``finished`` signal. Signal payload is the
    :class:`AnalysisResult` instance, unmodified."""
    analyzer = Analyzer([_OkExtractor()])
    worker = AnalysisWorker(analyzer, fixture_path("tiny.jpg"))

    captured: list[AnalysisResult] = []
    worker.finished.connect(captured.append)

    worker.run()

    assert len(captured) == 1
    assert "ok" in captured[0].results
    assert captured[0].results["ok"].data == {"hello": "world"}


def test_run_does_not_emit_failed_on_success() -> None:
    """Happy path emits ``finished`` exactly once and ``failed`` zero
    times. Asymmetry guards against a future refactor that double-emits."""
    analyzer = Analyzer([_OkExtractor()])
    worker = AnalysisWorker(analyzer, fixture_path("tiny.jpg"))

    finished_calls: list[AnalysisResult] = []
    failed_calls: list[str] = []
    worker.finished.connect(finished_calls.append)
    worker.failed.connect(failed_calls.append)

    worker.run()

    assert len(finished_calls) == 1
    assert failed_calls == []


# ---------------------------------------------------------------------------
# Defensive path — analyzer.analyze raises
# ---------------------------------------------------------------------------


def test_run_emits_failed_when_analyzer_raises() -> None:
    """If ``Analyzer.analyze`` raises, the worker catches and emits
    ``failed`` with a formatted error string. The format matches the
    orchestrator's error-string convention (``Type: message``) for
    consistency with what the CLI would show."""
    analyzer = _RaisingAnalyzer(RuntimeError("disk on fire"))
    worker = AnalysisWorker(analyzer, fixture_path("tiny.jpg"))  # type: ignore[arg-type]

    captured: list[str] = []
    worker.failed.connect(captured.append)

    worker.run()

    assert len(captured) == 1
    assert captured[0].startswith("RuntimeError:")
    assert "disk on fire" in captured[0]


def test_run_does_not_emit_finished_on_failure() -> None:
    """Failure path emits ``failed`` exactly once and ``finished`` zero
    times. Same single-signal-per-run contract as the happy path."""
    analyzer = _RaisingAnalyzer(RuntimeError("oops"))
    worker = AnalysisWorker(analyzer, fixture_path("tiny.jpg"))  # type: ignore[arg-type]

    finished_calls: list[AnalysisResult] = []
    failed_calls: list[str] = []
    worker.finished.connect(finished_calls.append)
    worker.failed.connect(failed_calls.append)

    worker.run()

    assert finished_calls == []
    assert len(failed_calls) == 1
