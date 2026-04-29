"""Hypothesis property tests for :func:`_dms_to_decimal` and :func:`_normalize`.

The conversion is pure math, but it operates on adversarial input
(EXIF tags from arbitrary files). Property-based testing surfaces the
edge cases hand-written tests miss — extreme rationals, empty bytes,
unicode-edge bytes, etc.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from pixel_probe.core.extractors.exif import _dms_to_decimal, _normalize

pytestmark = pytest.mark.property


# Realistic DMS bounds: degrees 0-180, minutes/seconds 0-59. We assume() valid
# longitude shape (total ≤ 180) where bounds-checks need it — see
# test_dms_to_decimal_within_bounds. Cases like (180°, 59', 59.999") are
# numerically valid as input but represent an out-of-range longitude.
_dms = st.tuples(
    st.floats(min_value=0, max_value=180, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0, max_value=59.999, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0, max_value=59.999, allow_nan=False, allow_infinity=False),
)


@given(dms=_dms, ref=st.sampled_from(["N", "S", "E", "W"]))
def test_dms_to_decimal_is_finite_for_valid_input(
    dms: tuple[float, float, float], ref: str
) -> None:
    """For any well-formed DMS triple + ref, the result is a finite float."""
    result = _dms_to_decimal(dms, ref)
    assert math.isfinite(result)


@given(dms=_dms)
def test_dms_to_decimal_sign_follows_ref(dms: tuple[float, float, float]) -> None:
    """N/E are non-negative; S/W are non-positive. The magnitude is the same."""
    n = _dms_to_decimal(dms, "N")
    s = _dms_to_decimal(dms, "S")
    e = _dms_to_decimal(dms, "E")
    w = _dms_to_decimal(dms, "W")

    assert n >= 0
    assert s <= 0
    assert e >= 0
    assert w <= 0
    assert n == pytest.approx(-s)
    assert e == pytest.approx(-w)


@given(dms=_dms, ref=st.sampled_from(["N", "S", "E", "W"]))
def test_dms_to_decimal_within_bounds(dms: tuple[float, float, float], ref: str) -> None:
    """For *valid* longitude-shaped DMS input (total ≤ 180°), the result
    stays in [-180, 180]. The ``assume`` filters out edge inputs like
    (180°, 59', 59.999") which are numerically valid as a tuple but
    represent an out-of-range longitude — the converter doesn't validate
    its input, just computes the math."""
    deg, minutes, seconds = dms
    assume(deg + minutes / 60 + seconds / 3600 <= 180)
    result = _dms_to_decimal(dms, ref)
    assert -180 <= result <= 180


@given(value=st.binary(min_size=0, max_size=200))
def test_normalize_bytes_never_raises(value: bytes) -> None:
    """``_normalize`` must never raise on any byte sequence — it's the last
    line of defense against weird Pillow tag values reaching JSON serialization.
    Result must be either a str (decoded) or a str (summary)."""
    result = _normalize(value)
    assert isinstance(result, str)


@given(value=st.binary(min_size=65, max_size=200))
def test_normalize_summarizes_long_bytes(value: bytes) -> None:
    """Anything over the inline cap is summarized — even if it would have
    been UTF-8 decodable. This is the MakerNote / adversarial-input gate."""
    result = _normalize(value)
    assert isinstance(result, str)
    assert result.startswith("<binary,")
    assert result.endswith("bytes>")
    assert str(len(value)) in result


@given(
    elements=st.lists(
        st.one_of(
            st.integers(),
            st.floats(allow_nan=False, allow_infinity=False),
            st.text(max_size=20),
        ),
        max_size=10,
    )
)
def test_normalize_tuple_recursion(elements: list[object]) -> None:
    """Tuples are recursively normalized — the contract used by GPS DMS
    triples and similar EXIF compound tags."""
    tup = tuple(elements)
    result = _normalize(tup)
    assert isinstance(result, tuple)
    assert len(result) == len(elements)
