"""Hypothesis property tests for the IPTC byte walkers.

The walkers (:func:`_iter_segments`, :func:`_iter_irbs`,
:func:`_iter_iim_records`) operate on adversarial input — JPEG/IRB/IIM
bytes from arbitrary files. The load-bearing invariant is that they
*never raise* on any byte sequence: malformed structure terminates the
walk cleanly, and bounds violations stop iteration rather than read
past end-of-buffer.

Hypothesis is the right hammer for this — hand-written tests cover
specific corruption shapes (truncated length, bad signature, oversized
size); property tests cover the long tail of bytes-in-the-wild.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pixel_probe.core.extractors.iptc import (
    _iter_iim_records,
    _iter_irbs,
    _iter_segments,
)

pytestmark = pytest.mark.property


# Bounded sizes keep test runtime sensible — IPTC blocks in the wild are
# rarely above a few KB, so 1024 is well above the realistic ceiling for
# coverage purposes.
_arbitrary_bytes = st.binary(min_size=0, max_size=1024)


@given(data=_arbitrary_bytes)
def test_iter_segments_never_raises(data: bytes) -> None:
    """For any byte sequence, the segment walker either yields nothing or
    yields well-formed ``(marker, payload)`` tuples — never an exception.
    This is the load-bearing safety property: garbage input from a corrupt
    or hostile file can't take down the parser."""
    for marker, payload in _iter_segments(data):
        assert isinstance(marker, int)
        assert 0 <= marker <= 0xFF
        assert isinstance(payload, bytes)


@given(data=_arbitrary_bytes)
def test_iter_segments_payloads_within_input(data: bytes) -> None:
    """Every yielded payload is a slice of the original buffer — no
    fabricated bytes, no out-of-bounds reads. Hypothesis-fuzzed equivalent
    of the hand-written truncation tests."""
    for _marker, payload in _iter_segments(data):
        assert payload in data


@given(data=_arbitrary_bytes)
def test_iter_irbs_never_raises(data: bytes) -> None:
    """Same no-raise invariant for the IRB walker — feed it any bytes and
    it must terminate cleanly. The IRB walker is the most fiddly of the
    three (variable-length Pascal name, pad-to-even on multiple fields)
    so this is the highest-value property test of the bunch."""
    for resource_id, block in _iter_irbs(data):
        assert isinstance(resource_id, int)
        assert 0 <= resource_id <= 0xFFFF
        assert isinstance(block, bytes)


@given(data=_arbitrary_bytes)
def test_iter_irbs_blocks_within_input(data: bytes) -> None:
    """Every yielded data block is a slice of the original buffer."""
    for _resource_id, block in _iter_irbs(data):
        assert block in data


@given(data=_arbitrary_bytes)
def test_iter_iim_records_never_raises(data: bytes) -> None:
    """No-raise invariant for the IIM record walker."""
    for record, dataset, value in _iter_iim_records(data):
        assert isinstance(record, int)
        assert isinstance(dataset, int)
        assert isinstance(value, bytes)
        assert 0 <= record <= 0xFF
        assert 0 <= dataset <= 0xFF


@given(data=_arbitrary_bytes)
def test_iter_iim_records_values_within_input(data: bytes) -> None:
    """Every yielded value is a slice of the original buffer — same
    out-of-bounds-protection property as the other two walkers."""
    for _record, _dataset, value in _iter_iim_records(data):
        assert value in data
