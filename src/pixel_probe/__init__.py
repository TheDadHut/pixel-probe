"""pixel-probe — desktop image-analysis tool for inspecting EXIF, IPTC, and XMP metadata."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pixel-probe")
except PackageNotFoundError:  # pragma: no cover — only hit when running uninstalled
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
