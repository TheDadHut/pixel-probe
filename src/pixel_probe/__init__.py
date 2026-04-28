"""pixel-probe — desktop image-analysis tool for inspecting EXIF, IPTC, and XMP metadata."""

from importlib.metadata import PackageNotFoundError, version

from .core import AnalysisResult, Analyzer
from .core.extractors import Extractor, ExtractorResult, FileInfo, FileInfoExtractor
from .exceptions import (
    CorruptMetadataError,
    DecompressionBombError,
    FileTooLargeError,
    MissingFileError,
    PixelProbeError,
    UnsupportedFormatError,
)

try:
    __version__ = version("pixel-probe")
except PackageNotFoundError:  # pragma: no cover — only hit when running uninstalled
    __version__ = "0.0.0+unknown"

__all__ = [
    "AnalysisResult",
    "Analyzer",
    "CorruptMetadataError",
    "DecompressionBombError",
    "Extractor",
    "ExtractorResult",
    "FileInfo",
    "FileInfoExtractor",
    "FileTooLargeError",
    "MissingFileError",
    "PixelProbeError",
    "UnsupportedFormatError",
    "__version__",
]
