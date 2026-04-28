"""Abstract extractor interface and the result envelope.

See ADR 0003 (hybrid result shape) for the design rationale and
ADR 0001 for the broader extractor-vs-analyzer split.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

__all__ = [
    "Extractor",
    "ExtractorResult",
]

T = TypeVar("T")


@dataclass(frozen=True)
class ExtractorResult(Generic[T]):
    """Envelope for a single extractor's output, parameterized on payload type ``T``.

    Built-in extractors specialize ``T`` to their declared payload shape,
    e.g. :class:`pixel_probe.core.extractors.file_info.FileInfo` for
    file-info, ``ExifData`` (a type alias for ``dict[str, Any]``) for EXIF.
    The ``data`` field is the typed payload at runtime; conversion to dict
    happens at serialization boundaries (CLI JSON output, GUI tree-builder).

    ``data`` is ``T | None`` because the orchestrator constructs error-only
    results (``data=None``) when an extractor raises. Successful extractions
    always populate ``data`` with a concrete ``T``.

    **Error model**

    - Extractors RAISE (:class:`pixel_probe.exceptions.PixelProbeError`
      subclasses) for catastrophic failures — file unreadable, unsupported
      format, decompression bomb. The orchestrator catches these.
    - Extractors return ERRORS in this field for partial-extraction issues
      — one bad tag, malformed sub-block. Extraction continues; the rest
      of the data still ships in ``data``.
    - WARNINGS are non-fatal anomalies the user should know about — e.g.
      an unrecognised charset that fell back to UTF-8 with replacement.

    ``warnings`` and ``errors`` are tuples (not lists) so both the reference
    *and* the contents are immutable, matching ``frozen=True`` on the dataclass.
    """

    extractor_name: str
    data: T | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def has_data(self) -> bool:
        """True iff extraction succeeded and produced a non-empty payload."""
        return self.data is not None and bool(self.data)


class Extractor(ABC, Generic[T]):
    """Abstract interface for a metadata extractor.

    Concrete extractors set ``name`` (a unique short identifier used as
    the result key in :class:`pixel_probe.core.analyzer.AnalysisResult`)
    and ``payload_type`` (the runtime type of the extractor's payload,
    useful for introspection and docs).

    Subclasses implement exactly one method, :meth:`extract`. This narrow
    interface is deliberate — the orchestrator depends only on what it needs
    (matrix decision: SOLID interface segregation).
    """

    name: str
    payload_type: type[T]

    @abstractmethod
    def extract(self, path: Path) -> ExtractorResult[T]:
        """Read metadata from ``path`` and return an :class:`ExtractorResult`.

        Raises a :class:`pixel_probe.exceptions.PixelProbeError` subclass
        on catastrophic failure. Use the result's ``errors`` / ``warnings``
        tuples for non-fatal issues.
        """
