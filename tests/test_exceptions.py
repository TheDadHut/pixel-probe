"""Exception-hierarchy contract tests."""

from __future__ import annotations

import pytest

from pixel_probe.exceptions import (
    CorruptMetadataError,
    DecompressionBombError,
    FileTooLargeError,
    PixelProbeError,
    UnsupportedFormatError,
)


def test_pixel_probe_error_is_exception() -> None:
    assert issubclass(PixelProbeError, Exception)


def test_all_subclasses_inherit_from_pixel_probe_error() -> None:
    """Every domain exception must inherit from the base — that's how callers
    catch the family with one ``except`` clause."""
    for cls in (
        UnsupportedFormatError,
        CorruptMetadataError,
        FileTooLargeError,
        DecompressionBombError,
    ):
        assert issubclass(cls, PixelProbeError)


def test_subclasses_are_distinct() -> None:
    """Catching one subclass mustn't catch siblings."""
    try:
        raise FileTooLargeError("test")
    except DecompressionBombError:
        msg = "FileTooLargeError caught as DecompressionBombError — siblings collide"
        raise AssertionError(msg) from None
    except FileTooLargeError:
        pass


def test_chained_exceptions_preserve_cause() -> None:
    """When we wrap a Pillow error in our own, ``__cause__`` must point at
    the original — debuggability hinges on this."""
    original = ValueError("original")

    def _wrap_and_raise() -> None:
        try:
            raise original
        except ValueError as e:
            raise DecompressionBombError("wrapped") from e

    with pytest.raises(DecompressionBombError) as exc_info:
        _wrap_and_raise()
    assert exc_info.value.__cause__ is original
