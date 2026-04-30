"""Sequential extractor orchestrator.

The :class:`Analyzer` runs a list of extractors against an image path and
returns an :class:`AnalysisResult` keyed by extractor name. See ADR 0004
(constructor DI vs plugin entry points) and ADR 0005 (sequential vs
threaded execution) for the design rationale; ADR 0011 documents the
catch-distinction between expected and unexpected exceptions.

Per-extractor exceptions are converted into error-only results so that one
bad parser doesn't kill the whole run. ``PixelProbeError`` subclasses are
the expected failure shape (file-level catastrophic failures) and are
caught silently. Any other ``Exception`` indicates a bug in the extractor;
it's logged via :mod:`logging` so the traceback is visible to operators
before being converted to error-in-result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pixel_probe.exceptions import PixelProbeError

from .extractors.base import Extractor, ExtractorResult
from .extractors.exif import ExifExtractor
from .extractors.file_info import FileInfoExtractor
from .extractors.iptc import IptcExtractor
from .extractors.xmp import XmpExtractor

__all__ = [
    "AnalysisResult",
    "Analyzer",
]

logger = logging.getLogger(__name__)


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

        v0.1: file_info + EXIF + IPTC + XMP. Order matters for downstream
        rendering (CLI / GUI tree both display in declared order);
        file-level metadata first, then format-specific.
        """
        return cls([FileInfoExtractor(), ExifExtractor(), IptcExtractor(), XmpExtractor()])

    def analyze(self, path: Path) -> AnalysisResult:
        """Run every extractor against ``path``; return the aggregate.

        Two catch tiers per ADR 0011:

        - :class:`~pixel_probe.exceptions.PixelProbeError` — expected
          extractor-side failures (missing file, file-too-large,
          decompression bomb). Caught silently and converted to
          error-only :class:`ExtractorResult`.
        - Any other :class:`Exception` — programming bug in the extractor.
          Logged via :mod:`logging` (with traceback) before being
          converted to error-in-result. The "BUG: " prefix on the error
          string makes the distinction visible to consumers.

        Catastrophic infra failures (e.g. :class:`KeyboardInterrupt`,
        :class:`SystemExit`) aren't ``Exception`` subclasses and propagate
        unchanged.
        """
        results: dict[str, ExtractorResult[Any]] = {}
        for extractor in self._extractors:
            try:
                results[extractor.name] = extractor.extract(path)
            except PixelProbeError as e:
                results[extractor.name] = ExtractorResult(
                    extractor.name,
                    data=None,
                    errors=(f"{type(e).__name__}: {e}",),
                )
            except Exception as e:
                logger.exception(
                    "Unexpected exception in extractor %r processing %r — likely a bug",
                    extractor.name,
                    path,
                )
                results[extractor.name] = ExtractorResult(
                    extractor.name,
                    data=None,
                    errors=(f"BUG: {type(e).__name__}: {e}",),
                )
        return AnalysisResult(str(path), results)
