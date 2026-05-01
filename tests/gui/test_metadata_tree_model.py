"""Tests for :class:`MetadataTreeModel` — pure model logic, no widgets.

These tests don't drive a Qt event loop. ``pytest-qt``'s session-scoped
``QApplication`` (auto-set by the ``qt_api`` config) is enough — model
construction and signal emission via ``begin/endResetModel`` are
synchronous.

Coverage strategy: build an :class:`AnalysisResult` covering the four
branches of :meth:`MetadataTreeModel._build_tree`:

- ``data is None`` (failure tier per ADR 0011)
- ``data == {}`` (success-but-empty)
- ``data`` is a populated dict (with nested dict + list values)
- ``data`` is a dataclass (``FileInfo``)

Plus warnings + errors render as italic rows. Plus the QAbstractItemModel
boilerplate (index/parent/rowCount/columnCount/data/flags/headerData)
gives correct shape under the standard Qt walk pattern.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QFont

from pixel_probe.core.analyzer import AnalysisResult
from pixel_probe.core.extractors.base import ExtractorResult
from pixel_probe.core.extractors.file_info import FileInfo
from pixel_probe.gui.widgets.metadata_tree import MetadataTreeModel, Node

# No ``gui`` marker — these tests don't drive an event loop. ``pytest-qt``'s
# session-scoped ``QApplication`` (auto-set by ``qt_api = "pyside6"`` in
# pyproject.toml) is enough for QObject construction and signal emission.
# The ``gui`` marker is reserved for tests that need an event loop (Phase 5b).


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_file_info() -> FileInfo:
    """Build a fully-populated FileInfo for tests that exercise the
    dataclass branch of _build_tree."""
    return FileInfo(
        path="<test-path>",
        size_bytes=1234,
        sha256="deadbeef",
        mtime_iso="2026-01-15T00:00:00+00:00",
        format="JPEG",
        mode="RGB",
        width=100,
        height=100,
    )


def _result_with(**results: ExtractorResult[object]) -> AnalysisResult:
    """Build an AnalysisResult from kwargs of named extractor results."""
    return AnalysisResult(path="<test-path>", results=dict(results))


# ---------------------------------------------------------------------------
# set_result + tree shape
# ---------------------------------------------------------------------------


def test_set_result_creates_one_top_level_node_per_extractor() -> None:
    """Top-level row count equals the number of extractors in the result.
    Order follows the orchestrator's declared order (Python dict insertion
    order; see ADR 0005)."""
    model = MetadataTreeModel()
    model.set_result(
        _result_with(
            file_info=ExtractorResult("file_info", _make_file_info()),
            exif=ExtractorResult("exif", {"Make": "Canon"}),
        )
    )

    assert model.rowCount() == 2
    # Section rows: column 0 should carry the extractor name, column 1 empty.
    assert model.index(0, 0).data() == "file_info"
    assert model.index(1, 0).data() == "exif"
    assert model.index(0, 1).data() == ""
    assert model.index(1, 1).data() == ""


def test_set_result_populates_dict_payload() -> None:
    """Dict-shaped data → key/value child rows under the section."""
    model = MetadataTreeModel()
    model.set_result(_result_with(exif=ExtractorResult("exif", {"Make": "Canon"})))

    section = model.index(0, 0)
    assert model.rowCount(section) == 1
    leaf = model.index(0, 0, section)
    assert leaf.data() == "Make"
    assert model.index(0, 1, section).data() == "Canon"


def test_set_result_renders_data_none_as_no_data_marker() -> None:
    """Failure tier (data=None) → single ``(no data)`` placeholder child
    so the section isn't visually empty under the header."""
    model = MetadataTreeModel()
    model.set_result(_result_with(exif=ExtractorResult("exif", errors=("oops",))))

    section = model.index(0, 0)
    # Two children: the "(no data)" placeholder + the error row.
    assert model.rowCount(section) == 2
    assert model.index(0, 0, section).data() == "(no data)"


def test_set_result_renders_empty_dict_as_empty_marker() -> None:
    """Success-but-empty (``data == {}``) → single ``(empty)`` placeholder."""
    model = MetadataTreeModel()
    model.set_result(_result_with(exif=ExtractorResult("exif", {})))

    section = model.index(0, 0)
    assert model.rowCount(section) == 1
    assert model.index(0, 0, section).data() == "(empty)"


def test_set_result_recurses_into_nested_dict() -> None:
    """EXIF's GPS sub-dict produces a sub-tree, not flattened keys —
    contrast with the CLI flattener which uses dot-keys for the same
    data. The tree shape preserves structure visually."""
    model = MetadataTreeModel()
    model.set_result(
        _result_with(
            exif=ExtractorResult(
                "exif",
                {"Make": "Canon", "gps": {"latitude": 37.5, "longitude": -122.4}},
            )
        )
    )

    section = model.index(0, 0)
    # Two top-level data children: "Make" (leaf) + "gps" (subtree)
    assert model.rowCount(section) == 2
    gps_index = model.index(1, 0, section)
    assert gps_index.data() == "gps"
    assert model.rowCount(gps_index) == 2
    assert model.index(0, 0, gps_index).data() == "latitude"
    assert model.index(0, 1, gps_index).data() == "37.5"


def test_set_result_renders_list_values_as_indexed_children() -> None:
    """IPTC's ``Keywords`` list becomes a parent row with ``[0]`` / ``[1]``
    / ``[2]`` indexed leaves — preserves order, makes each item
    individually selectable for the planned Ctrl+C copy in 5b."""
    model = MetadataTreeModel()
    model.set_result(
        _result_with(iptc=ExtractorResult("iptc", {"Keywords": ["alpha", "beta", "gamma"]}))
    )

    section = model.index(0, 0)
    keywords = model.index(0, 0, section)
    assert keywords.data() == "Keywords"
    assert model.rowCount(keywords) == 3
    for i, expected in enumerate(("alpha", "beta", "gamma")):
        assert model.index(i, 0, keywords).data() == f"[{i}]"
        assert model.index(i, 1, keywords).data() == expected


def test_set_result_handles_dataclass_payload() -> None:
    """``FileInfo`` (dataclass payload) flattens to its field/value rows
    via ``asdict``, the same as a plain dict would."""
    model = MetadataTreeModel()
    model.set_result(_result_with(file_info=ExtractorResult("file_info", _make_file_info())))

    section = model.index(0, 0)
    # FileInfo has 8 fields; rowCount equals the asdict size.
    assert model.rowCount(section) == 8
    # Spot-check a couple of fields by walking column 0.
    field_names = {model.index(i, 0, section).data() for i in range(8)}
    assert {"size_bytes", "sha256", "format"}.issubset(field_names)


def test_set_result_renders_warnings_and_errors_at_bottom() -> None:
    """Warnings + errors appear after the data rows so the data is at the
    top where the eye lands first. Both render with italic font."""
    model = MetadataTreeModel()
    model.set_result(
        _result_with(
            iptc=ExtractorResult(
                "iptc",
                {"Title": "x"},
                warnings=("PNG unsupported",),
                errors=("nope",),
            )
        )
    )

    section = model.index(0, 0)
    # 1 data + 1 warning + 1 error = 3 rows under the section.
    assert model.rowCount(section) == 3
    assert model.index(0, 0, section).data() == "Title"
    assert model.index(1, 0, section).data() == "warning"
    assert model.index(1, 1, section).data() == "PNG unsupported"
    assert model.index(2, 0, section).data() == "error"
    assert model.index(2, 1, section).data() == "nope"


def test_set_result_handles_unknown_payload_shape_via_repr() -> None:
    """Defensive: a payload that's neither dict nor dataclass falls
    through to ``repr()`` — single child row carrying the repr string,
    so the user sees something rather than an empty section."""
    model = MetadataTreeModel()
    model.set_result(_result_with(custom=ExtractorResult("custom", "raw payload")))

    section = model.index(0, 0)
    assert model.rowCount(section) == 1
    assert model.index(0, 0, section).data() == "'raw payload'"


def test_set_result_replaces_previous_tree() -> None:
    """Calling ``set_result`` again fully replaces the tree — emits the
    standard reset signals so views/proxies discard cached state. The
    previous extractor sections must be gone, not appended-to."""
    model = MetadataTreeModel()
    model.set_result(_result_with(exif=ExtractorResult("exif", {"a": 1})))
    assert model.rowCount() == 1

    model.set_result(
        _result_with(
            file_info=ExtractorResult("file_info", _make_file_info()),
            iptc=ExtractorResult("iptc", {"Title": "x"}),
        )
    )
    assert model.rowCount() == 2
    assert model.index(0, 0).data() == "file_info"
    assert model.index(1, 0).data() == "iptc"


# ---------------------------------------------------------------------------
# QAbstractItemModel boilerplate — Qt's walking contract
# ---------------------------------------------------------------------------


def test_column_count_is_two_everywhere() -> None:
    """Two columns (Field, Value) at every level of the tree, including
    the invisible-root level."""
    model = MetadataTreeModel()
    model.set_result(_result_with(exif=ExtractorResult("exif", {"a": 1})))

    assert model.columnCount() == 2  # root level
    assert model.columnCount(model.index(0, 0)) == 2  # section level
    assert model.columnCount(model.index(0, 0, model.index(0, 0))) == 2  # leaf level


def test_header_data_returns_field_value_labels() -> None:
    """Horizontal headers carry the column labels; everything else is None."""
    model = MetadataTreeModel()

    assert model.headerData(0, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) == "Field"
    assert model.headerData(1, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) == "Value"
    # Vertical headers (row numbers) suppressed.
    assert model.headerData(0, Qt.Orientation.Vertical, Qt.ItemDataRole.DisplayRole) is None
    # Non-display roles return None.
    assert model.headerData(0, Qt.Orientation.Horizontal, Qt.ItemDataRole.FontRole) is None


def test_index_returns_invalid_for_out_of_range_row() -> None:
    """Asking for a row past the end yields an invalid index, not a
    crash. Defensive against stale index references after a reset."""
    model = MetadataTreeModel()
    model.set_result(_result_with(exif=ExtractorResult("exif", {"a": 1})))

    assert not model.index(99, 0).isValid()
    assert not model.index(0, 99).isValid()  # column out of range


def test_parent_of_top_level_is_invalid() -> None:
    """Section rows have no visible parent (their parent is the invisible
    root). Per Qt convention that means an invalid QModelIndex."""
    model = MetadataTreeModel()
    model.set_result(_result_with(exif=ExtractorResult("exif", {"a": 1})))

    assert not model.parent(model.index(0, 0)).isValid()


def test_parent_of_leaf_returns_section_index() -> None:
    """Walking from a leaf back to its section row produces the same
    QModelIndex you'd get by calling index(0, 0) directly. Confirms the
    parent pointer plumbing is consistent."""
    model = MetadataTreeModel()
    model.set_result(_result_with(exif=ExtractorResult("exif", {"a": 1})))

    section = model.index(0, 0)
    leaf = model.index(0, 0, section)
    parent = model.parent(leaf)
    assert parent.isValid()
    assert parent.data() == "exif"
    assert parent.row() == section.row()


def test_parent_of_invalid_index_is_invalid() -> None:
    """Invariant: parent(invalid) returns invalid. Qt views call this on
    the invisible root index during normal walking; a crash here would
    blow up any view that attaches to the model."""
    model = MetadataTreeModel()
    assert not model.parent(QModelIndex()).isValid()


def test_data_returns_none_for_invalid_index() -> None:
    """Defensive: querying data on an invalid index returns None rather
    than dereferencing a null pointer."""
    model = MetadataTreeModel()
    assert model.data(QModelIndex()) is None


def test_data_renders_warning_rows_with_italic_font() -> None:
    """Warning rows surface an italic :class:`QFont` for the FontRole
    so views render them differently from data rows. Errors do too;
    same code path."""
    model = MetadataTreeModel()
    model.set_result(_result_with(iptc=ExtractorResult("iptc", {}, warnings=("w",))))

    section = model.index(0, 0)
    # Section has children: (empty) placeholder + warning. Warning is row 1.
    warning_index = model.index(1, 0, section)
    font = model.data(warning_index, Qt.ItemDataRole.FontRole)
    assert isinstance(font, QFont)
    assert font.italic()


def test_data_returns_no_font_for_data_rows() -> None:
    """Data rows don't carry a FontRole override — views fall back to
    their default font. Without this filter the FontRole role would
    return None for italic=False nodes; we want None on non-italic."""
    model = MetadataTreeModel()
    model.set_result(_result_with(exif=ExtractorResult("exif", {"a": 1})))

    section = model.index(0, 0)
    leaf = model.index(0, 0, section)
    assert model.data(leaf, Qt.ItemDataRole.FontRole) is None


def test_data_returns_none_for_unrecognized_role() -> None:
    """Roles other than Display / Font return None — keeps the model
    contract narrow and predictable."""
    model = MetadataTreeModel()
    model.set_result(_result_with(exif=ExtractorResult("exif", {"a": 1})))

    section = model.index(0, 0)
    leaf = model.index(0, 0, section)
    assert model.data(leaf, Qt.ItemDataRole.ToolTipRole) is None


def test_flags_marks_cells_selectable_and_enabled() -> None:
    """Per the model contract: every valid index is selectable +
    enabled (load-bearing for the Phase 5b Ctrl+C affordance) but
    not editable (this is a read-only view of an analysis result)."""
    model = MetadataTreeModel()
    model.set_result(_result_with(exif=ExtractorResult("exif", {"a": 1})))

    flags = model.flags(model.index(0, 0))
    assert flags & Qt.ItemFlag.ItemIsEnabled
    assert flags & Qt.ItemFlag.ItemIsSelectable
    assert not (flags & Qt.ItemFlag.ItemIsEditable)


def test_flags_returns_no_flags_for_invalid_index() -> None:
    """An invalid index has no flags — Qt's default for "this isn't a
    real cell"."""
    model = MetadataTreeModel()
    assert model.flags(QModelIndex()) == Qt.ItemFlag.NoItemFlags


def test_row_count_returns_zero_for_column_one() -> None:
    """Per Qt convention only column 0 carries children. The Value
    column reports zero children even when the cell sits on a
    parent-shaped row. Confirms QSortFilterProxyModel + nested-tree
    walking won't double-count."""
    model = MetadataTreeModel()
    model.set_result(_result_with(exif=ExtractorResult("exif", {"a": 1})))

    section_col_one = model.index(0, 1)  # Value column of section row
    assert model.rowCount(section_col_one) == 0


# ---------------------------------------------------------------------------
# Node dataclass — narrow surface, but worth pinning a couple invariants
# ---------------------------------------------------------------------------


def test_node_default_factory_creates_independent_lists() -> None:
    """Each Node gets its own children list — no shared mutable
    default state. Default-factory pattern; this test guards against
    a future hand-edit accidentally introducing a class-level default."""
    a = Node(key="a")
    b = Node(key="b")
    a.children.append(Node(key="x", parent=a))
    assert b.children == []
