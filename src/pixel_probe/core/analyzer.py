"""Sequential extractor orchestrator.

The :class:`Analyzer` runs a list of extractors against an image path and
returns an :class:`AnalysisResult` keyed by extractor name. See ADR 0004
(constructor DI vs plugin entry points) and ADR 0005 (sequential vs
threaded execution) for the design rationale.

Per-extractor exceptions are converted into error-only results so that one
bad parser doesn't kill the whole run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .extractors.base import Extractor, ExtractorResult
from .extractors.exif import ExifExtractor
from .extractors.file_info import FileInfoExtractor
from .extractors.iptc import IptcExtractor

__all__ = [
    "AnalysisResult",
    "Analyzer",
]


@dataclass(frozen=True)
class AnalysisResult:
    """Aggregate output of all extractors that ran on a single file.

    ``results`` is keyed by :attr:`Extractor.name`. Order is the declared
    order of extractors passed to the :class:`Analyzer` (Python dicts
    preserve insertion order since 3.7), which matters for downstream
    rendering (CLI / GUI tree both display extractors in this order).
    """

    path: str
    results: dict[str, ExtractorResult[Any]]


class Analyzer:
    """Sequentially run a fixed list of extractors against a path.

    Construct with an explicit extractor list, or use :meth:`default` for
    the built-in set:

    .. code-block:: python

        analyzer = Analyzer.default()
        result = analyzer.analyze(Path("photo.jpg"))

    Custom analyzers compose their own list — that's the extension story
    (matrix decision: SOLID dependency inversion). No plugin discovery,
    no entry points: just constructor injection.
    """

    def __init__(self, extractors: list[Extractor[Any]]) -> None:
        # Defensive copy: holding the caller's list directly would let them
        # mutate our internal state through the original reference.
        self._extractors = list(extractors)

    @classmethod
    def default(cls) -> Analyzer:
        """Return an :class:`Analyzer` configured with the built-in extractors.

        v0.1: file_info + EXIF + IPTC. Phase 3b adds XMP — it lands here.
        Order matters for downstream rendering (CLI / GUI tree both display
        in declared order); file-level metadata first, then format-specific.
        """
        return cls([FileInfoExtractor(), ExifExtractor(), IptcExtractor()])

    def analyze(self, path: Path) -> AnalysisResult:
        """Run every extractor against ``path``; return the aggregate.

        Any :class:`Exception` raised by an individual extractor is caught
        and wrapped in an error-only :class:`ExtractorResult` — one bad
        parse doesn't stop the rest. Catastrophic infra failures
        (e.g. ``KeyboardInterrupt``) propagate unchanged.
        """
        results: dict[str, ExtractorResult[Any]] = {}
        for extractor in self._extractors:
            try:
                results[extractor.name] = extractor.extract(path)
            except Exception as e:  # noqa: BLE001 - orchestrator is the catch-all boundary
                results[extractor.name] = ExtractorResult(
                    extractor.name,
                    data=None,
                    errors=(f"{type(e).__name__}: {e}",),
                )
        return AnalysisResult(str(path), results)
