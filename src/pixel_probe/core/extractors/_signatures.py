"""Shared image-format signature detection.

Used by extractors to distinguish "wrong file type entirely" (non-image)
from "image, but a format this extractor doesn't handle" (e.g. IPTC
handed a PNG). The two cases get different severities on the result
envelope per ADR 0011's three-tier classification: non-image → error,
image-but-wrong-format → warning.

The check is a cheap byte-prefix match against the leading magic bytes
of widely-deployed image formats. Not exhaustive — anything Pillow can
open via heuristic detection (sniffing for less-common formats) is
intentionally out of scope; the goal here is "is this almost certainly
an image?", not "is this every possible image."
"""

from __future__ import annotations

from typing import Final

__all__ = ["is_known_image_format"]

#: Leading byte sequences for the image formats common enough that a
#: pixel-probe user is plausibly running against them. Order doesn't
#: matter; ``bytes.startswith`` accepts a tuple and returns on the first
#: hit.
_KNOWN_IMAGE_SIGNATURES: Final[tuple[bytes, ...]] = (
    b"\xff\xd8",  # JPEG SOI
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"GIF87a",
    b"GIF89a",
    b"II*\x00",  # TIFF, little-endian
    b"MM\x00*",  # TIFF, big-endian
    b"BM",  # BMP
    b"RIFF",  # RIFF container — covers WebP and a few others
)


def is_known_image_format(raw: bytes) -> bool:
    """True iff ``raw`` begins with the magic bytes of a recognized image
    format.

    Used by extractors to decide warning-vs-error severity for
    format-mismatch cases. A ``True`` return means "the user gave us an
    image, just not one this extractor handles" → warning. A ``False``
    return means "the user gave us something that isn't an image at
    all" → error.
    """
    return raw.startswith(_KNOWN_IMAGE_SIGNATURES)
