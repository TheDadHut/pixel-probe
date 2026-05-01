"""Off-UI-thread worker that runs :meth:`Analyzer.analyze` and emits
result signals.

Uses the **QObject + moveToThread** pattern rather than subclassing
``QThread`` — recommended by the Qt documentation since 4.8 (the
subclass-QThread pattern is a common source of lifecycle bugs). See
ADR 0013 for the full reasoning.

The wiring lives in Phase 5b's ``MainWindow._start_analysis``:

.. code-block:: python

    self._thread = QThread(self)
    self._worker = AnalysisWorker(self._analyzer, path)
    self._worker.moveToThread(self._thread)

    self._thread.started.connect(self._worker.run)
    self._worker.finished.connect(self._on_analysis_done)
    self._worker.failed.connect(self._on_analysis_failed)

    # Wind down: worker.finished → thread.quit → cleanup
    self._worker.finished.connect(self._thread.quit)
    self._worker.failed.connect(self._thread.quit)
    self._thread.finished.connect(self._worker.deleteLater)
    self._thread.finished.connect(self._thread.deleteLater)
    self._thread.start()

The worker's :meth:`run` emits exactly one of ``finished`` /
``failed`` per invocation, never both. ``failed`` is rarely fired in
practice — :class:`Analyzer` per ADR 0011 catches both
:class:`PixelProbeError` and unexpected ``Exception`` internally and
converts them to error-in-result; the only path to ``failed`` is an
infrastructure-level failure (e.g., ``Analyzer.analyze`` itself failing
to construct a result). Wiring it remains worthwhile as a defensive
boundary for the GUI.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from pixel_probe.core.analyzer import AnalysisResult, Analyzer

__all__ = ["AnalysisWorker"]


class AnalysisWorker(QObject):
    """Run an analysis on a worker thread; emit the result via signal.

    One-shot: construct with ``(analyzer, path)``, ``moveToThread``,
    connect signals, and start the thread. The worker emits exactly one
    of :attr:`finished` / :attr:`failed` per :meth:`run` invocation,
    then is no longer used (caller schedules it for ``deleteLater``).

    Signals carry plain Python objects rather than custom types so that
    Qt's queued-signal serialization across the thread boundary doesn't
    need any registration boilerplate (``qRegisterMetaType``).
    """

    #: Emitted with the :class:`AnalysisResult` when extraction completes
    #: normally. ``object`` rather than the concrete type so PySide6's
    #: signal-type system doesn't need the metatype registered.
    finished = Signal(object)

    #: Emitted with a single error string when :meth:`Analyzer.analyze`
    #: itself raised an unexpected exception. In normal flow this never
    #: fires — the analyzer catches everything internally per ADR 0011.
    #: Kept as a defensive boundary for infrastructure failures.
    failed = Signal(str)

    def __init__(self, analyzer: Analyzer, path: Path) -> None:
        # No QObject parent — the worker is owned by its QThread (after
        # ``moveToThread``); a Qt-side parent on a different thread would
        # be a bug.
        super().__init__()
        self._analyzer = analyzer
        self._path = path

    @Slot()
    def run(self) -> None:
        """Run the analysis. Connected to ``QThread.started`` by the
        caller; runs once on the worker thread. Emits exactly one signal."""
        try:
            result: AnalysisResult = self._analyzer.analyze(self._path)
        except Exception as e:  # noqa: BLE001 — defensive boundary, see module docstring
            self.failed.emit(f"{type(e).__name__}: {e}")
            return
        self.finished.emit(result)
