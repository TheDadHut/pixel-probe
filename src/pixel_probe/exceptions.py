"""Custom exception hierarchy for pixel-probe.

Library code never raises bare ``Exception`` or ``ValueError`` — every error
is a :class:`PixelProbeError` subclass so callers can ``except PixelProbeError``
to filter the project's errors from third-party noise. See ADR 0003 and the
"Error model" section of :class:`pixel_probe.core.extractors.base.ExtractorResult`
for the broader contract.

The four named subclasses cover catastrophic, file-level failures that
extractors raise out of :meth:`Extractor.extract`. Partial-extraction
issues (e.g., malformed metadata block, unsupported per-extractor format)
are surfaced as ``errors`` / ``warnings`` on the
:class:`~pixel_probe.core.extractors.base.ExtractorResult` envelope rather
than raised — see ADR 0003's error model and ADR 0006 for the rationale.
"""

from __future__ import annotations

__all__ = [
    "DecompressionBombError",
    "FileTooLargeError",
    "MissingFileError",
    "PixelProbeError",
]


class PixelProbeError(Exception):
    """Base class for all pixel-probe errors."""


class MissingFileError(PixelProbeError):
    """Raised when an extractor's input path doesn't exist or isn't a regular file.

    Kept inside the :class:`PixelProbeError` hierarchy so a single
    ``except PixelProbeError`` clause catches all extractor-side failures.
    """


class FileTooLargeError(PixelProbeError):
    """Raised when a file exceeds the configured max size.

    DoS / decompression-bomb protection: refuse to read very large files
    before any parsing begins. See
    :data:`pixel_probe.core.extractors.file_info.MAX_FILE_SIZE_BYTES`.
    """


class DecompressionBombError(PixelProbeError):
    """Raised when an image's pixel count exceeds the configured maximum.

    Wraps Pillow's :class:`PIL.Image.DecompressionBombError` (the original
    is chained via ``__cause__``). See
    :data:`pixel_probe.core.extractors.file_info.MAX_IMAGE_PIXELS`.
    """
