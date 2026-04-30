"""pixel-probe — desktop image-analysis tool for inspecting EXIF, IPTC, and XMP metadata."""

from importlib.metadata import PackageNotFoundError, version

from .core import AnalysisResult, Analyzer
from .core.extractors import (
    ExifData,
    ExifExtractor,
    Extractor,
    ExtractorResult,
    FileInfo,
    FileInfoExtractor,
)
from .exceptions import (
    DecompressionBombError,
    FileTooLargeError,
    MissingFileError,
    PixelProbeError,
)

try:
    __version__ = version("pixel-probe")
except PackageNotFoundError:  # pragma: no cover — only hit when running uninstalled
    __version__ = "0.0.0+unknown"

__all__ = [
    "AnalysisResult",
    "Analyzer",
    "DecompressionBombError",
    "ExifData",
    "ExifExtractor",
    "Extractor",
    "ExtractorResult",
    "FileInfo",
    "FileInfoExtractor",
    "FileTooLargeError",
    "MissingFileError",
    "PixelProbeError",
    "__version__",
]
