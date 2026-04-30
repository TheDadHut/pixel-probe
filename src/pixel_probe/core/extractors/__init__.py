"""Metadata extractors — read embedded bytes from image files (EXIF, IPTC, XMP, file info)."""

from .base import Extractor, ExtractorResult
from .exif import ExifData, ExifExtractor
from .file_info import FileInfo, FileInfoExtractor
from .iptc import IptcData, IptcExtractor

__all__ = [
    "ExifData",
    "ExifExtractor",
    "Extractor",
    "ExtractorResult",
    "FileInfo",
    "FileInfoExtractor",
    "IptcData",
    "IptcExtractor",
]
