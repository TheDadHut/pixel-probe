"""Metadata extractors — read embedded bytes from image files (EXIF, IPTC, XMP, file info)."""

from .base import Extractor, ExtractorResult
from .file_info import FileInfo, FileInfoExtractor

__all__ = [
    "Extractor",
    "ExtractorResult",
    "FileInfo",
    "FileInfoExtractor",
]
