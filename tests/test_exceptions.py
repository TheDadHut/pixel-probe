"""Exception-hierarchy contract tests."""

from __future__ import annotations

import pytest

from pixel_probe.exceptions import (
    DecompressionBombError,
    FileTooLargeError,
    MissingFileError,
    PixelProbeError,
)


def test_pixel_probe_error_is_exception() -> None:
    assert issubclass(PixelProbeError, Exception)


def test_all_subclasses_inherit_from_pixel_probe_error() -> None:
    """Every domain exception must inherit from the base — that's how callers
    catch the family with one ``except`` clause."""
    for cls in (
        MissingFileError,
        FileTooLargeError,
        DecompressionBombError,
    ):
        assert issubclass(cls, PixelProbeError)


def test_subclasses_are_distinct() -> None:
    """Catching one subclass mustn't catch siblings — pairwise check across
    all three concrete subclasses. If a future class inherits from another
    by mistake (transitive subclass relationship), this test catches it."""
    siblings = (MissingFileError, FileTooLargeError, DecompressionBombError)
    for raised_cls in siblings:
        for catch_cls in siblings:
            if raised_cls is catch_cls:
                continue
            try:
                raise raised_cls("test")
            except catch_cls:
                msg = f"{raised_cls.__name__} was caught as {catch_cls.__name__} — siblings collide"
                raise AssertionError(msg) from None
            except raised_cls:
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
