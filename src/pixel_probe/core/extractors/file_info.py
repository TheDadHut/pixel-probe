"""File-level metadata: size, hash, format, dimensions, mtime.

This is the simplest concrete :class:`Extractor` and lands first deliberately
— it validates the whole pipeline (ABC → orchestrator → result envelope)
before EXIF/IPTC/XMP build on it.

Adversarial-input handling:

- ``MAX_FILE_SIZE_BYTES`` rejects huge files before any read begins.
- ``MAX_IMAGE_PIXELS`` lowers Pillow's decompression-bomb threshold below
  the library default. Pillow's :class:`PIL.Image.DecompressionBombError`
  is converted to our :class:`DecompressionBombError` so callers can catch
  by category.

Both are :data:`typing.Final` — the values are tunable via PR (or future
config) but never reassigned at runtime, except via the save/restore dance
required for Pillow's module-level ``MAX_IMAGE_PIXELS``.
"""

from __future__ import annotations

import threading
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Final

from PIL import Image, UnidentifiedImageError

from pixel_probe.exceptions import DecompressionBombError, FileTooLargeError, MissingFileError

from .base import Extractor, ExtractorResult

__all__ = [
    "MAX_FILE_SIZE_BYTES",
    "MAX_IMAGE_PIXELS",
    "FileInfo",
    "FileInfoExtractor",
]

# ``Image.MAX_IMAGE_PIXELS`` is process-global; the save/restore dance below
# is not thread-safe on its own. This lock serializes overlapping ``extract``
# calls so a concurrent caller can't observe (or worse, restore) a stale
# previous-value. Sequential v0.1 doesn't strictly need this, but the GUI
# worker (Phase 5) calls ``analyze`` from a background thread — a second
# request kicked off before the first finishes would race without the lock.
_PILLOW_MAX_PIXELS_LOCK = threading.Lock()

#: Refuse to read files larger than this. 500 MB covers any reasonable photo;
#: anything bigger is almost certainly a bomb or wrong file type.
MAX_FILE_SIZE_BYTES: Final = 500 * 1024 * 1024

#: Pillow's default DecompressionBomb threshold is ~178 MP. We tighten to
#: 100 MP — covers any 12000x8000 photo (current high-end DSLR territory)
#: with headroom, while rejecting obvious bombs.
MAX_IMAGE_PIXELS: Final = 100_000_000


@dataclass(frozen=True)
class FileInfo:
    """Concrete payload for :class:`FileInfoExtractor`.

    All fields are optional past ``path`` / ``size_bytes`` / ``sha256`` /
    ``mtime_iso``: the image-format fields (``format``, ``mode``, ``width``,
    ``height``) are ``None`` when the file isn't a recognized image.
    """

    path: str
    size_bytes: int
    sha256: str
    mtime_iso: str
    format: str | None
    mode: str | None
    width: int | None
    height: int | None


class FileInfoExtractor(Extractor[FileInfo]):
    """Extract file-level metadata: size, SHA-256, mtime, format, dimensions.

    Pure-input idempotent: same file → same :class:`FileInfo` every call.
    Side effects are confined to the SHA-256 and Pillow ``open`` reads.
    """

    name = "file_info"
    payload_type = FileInfo

    def extract(self, path: Path) -> ExtractorResult[FileInfo]:
        if not path.is_file():
            raise MissingFileError(f"Not a file: {path}")

        stat = path.stat()
        if stat.st_size > MAX_FILE_SIZE_BYTES:
            raise FileTooLargeError(
                f"{path} is {stat.st_size:,} bytes; max is {MAX_FILE_SIZE_BYTES:,}"
            )

        warning_messages: list[str] = []
        # SHA-256 is computed before the image-format check because we want to
        # populate the file-level fields (size, hash, mtime) for non-image inputs
        # too — only the image-specific fields go ``None`` in that case.
        digest = self._sha256(path)
        mtime_iso = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()

        fmt: str | None = None
        mode: str | None = None
        width: int | None = None
        height: int | None = None

        # Save/restore Pillow's module-level threshold under a lock so concurrent
        # extract() calls don't race on the global state. See module-level comment.
        with _PILLOW_MAX_PIXELS_LOCK:
            previous_max = Image.MAX_IMAGE_PIXELS
            Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
            try:
                # Pillow only RAISES DecompressionBombError above 2x MAX_IMAGE_PIXELS;
                # below that it emits DecompressionBombWarning. Escalate the warning
                # to an error so bombs are caught at the configured threshold, not 2x it.
                with warnings.catch_warnings():
                    warnings.filterwarnings("error", category=Image.DecompressionBombWarning)
                    try:
                        with Image.open(path) as img:
                            fmt, mode = img.format, img.mode
                            width, height = img.size
                    except UnidentifiedImageError:
                        warning_messages.append("File is not a recognized image format")
                    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as e:
                        raise DecompressionBombError(str(e)) from e
            finally:
                Image.MAX_IMAGE_PIXELS = previous_max

        info = FileInfo(
            path=str(path),
            size_bytes=stat.st_size,
            sha256=digest,
            mtime_iso=mtime_iso,
            format=fmt,
            mode=mode,
            width=width,
            height=height,
        )
        return ExtractorResult(self.name, info, tuple(warning_messages))

    @staticmethod
    def _sha256(path: Path, chunk_size: int = 65536) -> str:
        """Streaming SHA-256 — never loads the whole file into memory."""
        h = sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(chunk_size), b""):
                h.update(block)
        return h.hexdigest()
