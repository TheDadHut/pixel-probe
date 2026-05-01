"""Tree model for displaying :class:`AnalysisResult` in a ``QTreeView``.

This module ships the **model** in Phase 5a; the matching ``MetadataTreeView``
arrives in Phase 5b. Splitting model from view lets the model be unit-
tested without instantiating widgets — pure logic, no event loop required.

The model is a custom :class:`QAbstractItemModel` rather than the
convenience :class:`QStandardItemModel`. The trade-off: ~120 extra lines
of plumbing in exchange for direct control over the tree shape, font
roles for warnings/errors, and a clean read-only contract that
:class:`QSortFilterProxyModel` can wrap without surprises. ADR 0001
calls this out as the load-bearing portfolio piece on the GUI side.

**Tree shape** (one top-level node per extractor in declared order):

- ``file_info`` ─┬─ size_bytes : 1234
- ``exif``      ─┼─ Make       : TestCorp
- ``iptc``      ─┼─ Keywords   ─┬─ [0]: alpha
- ``xmp``       ─┘              └─ [1]: beta
                  ├─ gps        ─┬─ latitude  : 37.5
                  │              └─ longitude : -122.4
                  ├─ warning    : (italic) ...
                  └─ error      : (italic) ...

Construction rules in :meth:`MetadataTreeModel._build_tree`:

- ``data is None`` (failure per ADR 0011) → single ``(no data)`` child.
- ``data == {}`` (success-but-empty) → single ``(empty)`` child.
- ``data`` is a dict → key/value children, recursing into nested dicts;
  list values become indexed children (``[0]`` etc.).
- ``data`` is a dataclass (``FileInfo``) → ``asdict()``, treat as dict.
- Warnings and errors get italic-styled child rows at the bottom of the
  extractor's section so the user can see them in-tree without leaving
  the column layout.

The model is **read-only between** :meth:`set_result` calls — the only
mutating operation. ``MainWindow`` (Phase 5b) calls ``set_result`` from
the worker's ``finished`` slot. ``QSortFilterProxyModel`` can wrap this
model safely; no row-insertion / row-removal signals fire outside the
``beginResetModel`` / ``endResetModel`` pair.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QFont

from pixel_probe.core.analyzer import AnalysisResult
from pixel_probe.core.extractors.base import ExtractorResult

__all__ = ["MetadataTreeModel", "Node"]

#: Type alias for Qt's "index parameter" — Qt accepts either form everywhere
#: the index API takes a parameter; PySide6's stubs reflect that. Override
#: signatures must use the union to satisfy LSP per mypy strict.
_Index = QModelIndex | QPersistentModelIndex

#: Column indices. Two columns: the field name (key) and its display value.
_COLUMN_FIELD = 0
_COLUMN_VALUE = 1
_HEADERS: tuple[str, str] = ("Field", "Value")


@dataclass
class Node:
    """A single row in the metadata tree.

    Not frozen — :meth:`MetadataTreeModel._build_tree` mutates ``children``
    during construction. After ``set_result`` returns, the tree is
    effectively immutable until the next reset.

    The ``parent`` back-reference is load-bearing for ``QAbstractItemModel.parent``
    — Qt needs to walk from any index back to its parent index, and the
    fastest way is a direct pointer.
    """

    key: str
    value: str = ""
    children: list[Node] = field(default_factory=list)
    parent: Node | None = None
    italic: bool = False  # rendered with italic font role (warnings + errors)


class MetadataTreeModel(QAbstractItemModel):
    """Read-only tree model wrapping an :class:`AnalysisResult`.

    Use :meth:`set_result` to (re)load the tree; everything else is
    read-only. The model holds an invisible root :class:`Node` whose
    children are the extractor sections.
    """

    def __init__(self, parent: QAbstractItemModel | None = None) -> None:
        # The cast keeps mypy happy — QAbstractItemModel.__init__ wants a
        # QObject parent, and QAbstractItemModel is a QObject subclass.
        super().__init__(parent)
        self._root: Node = Node(key="")

    # -- public mutator --------------------------------------------------------

    def set_result(self, result: AnalysisResult) -> None:
        """Replace the model's tree with one built from ``result``.

        Full reset semantics: emits ``modelAboutToBeReset`` /
        ``modelReset`` so attached views and proxy models discard cached
        state cleanly. The expected caller is ``MainWindow``'s slot for
        the worker's ``finished`` signal."""
        self.beginResetModel()
        self._root = self._build_tree(result)
        self.endResetModel()

    # -- tree construction (private) ------------------------------------------

    def _build_tree(self, result: AnalysisResult) -> Node:
        """Walk the result envelope and produce the tree rooted at an
        invisible empty node. Per-extractor children appear in declared
        order (orchestrator's insertion order; see ADR 0005)."""
        root = Node(key="")
        for name, entry in result.results.items():
            section = Node(key=name, parent=root)
            root.children.append(section)
            self._populate_section(entry, section)
        return root

    def _populate_section(self, entry: ExtractorResult[Any], section: Node) -> None:
        """Append rows under one extractor section: data rows, then
        warnings, then errors (in that order so the data is at the top
        where the eye lands first)."""
        data = entry.data
        if data is None:
            section.children.append(Node(key="(no data)", parent=section))
        elif is_dataclass(data) and not isinstance(data, type):
            self._add_dict_children(asdict(data), section)
        elif isinstance(data, dict):
            if data:
                self._add_dict_children(data, section)
            else:
                section.children.append(Node(key="(empty)", parent=section))
        else:
            # Defensive: unknown payload shape from a custom extractor.
            section.children.append(Node(key=repr(data), parent=section))

        for warning in entry.warnings:
            section.children.append(Node(key="warning", value=warning, parent=section, italic=True))
        for err in entry.errors:
            section.children.append(Node(key="error", value=err, parent=section, italic=True))

    def _add_dict_children(self, mapping: dict[str, Any], parent_node: Node) -> None:
        """Recurse a dict into Node children. Dicts become subtrees,
        lists become indexed children (``[0]``, ``[1]``, …), other
        values become leaves."""
        for key, value in mapping.items():
            if isinstance(value, dict):
                child = Node(key=str(key), parent=parent_node)
                parent_node.children.append(child)
                self._add_dict_children(value, child)
            elif isinstance(value, list):
                child = Node(key=str(key), parent=parent_node)
                parent_node.children.append(child)
                for index, item in enumerate(value):
                    child.children.append(Node(key=f"[{index}]", value=str(item), parent=child))
            else:
                parent_node.children.append(
                    Node(key=str(key), value=str(value), parent=parent_node)
                )

    # -- QAbstractItemModel API -----------------------------------------------

    def index(
        self,
        row: int,
        column: int,
        parent: _Index = QModelIndex(),
    ) -> QModelIndex:
        """Build a :class:`QModelIndex` for ``(row, column)`` under ``parent``.

        The internal pointer is the child :class:`Node` — we'll use it
        in :meth:`parent` and :meth:`data` to navigate without re-walking.
        """
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        parent_node = self._node(parent)
        if row >= len(parent_node.children):  # pragma: no cover
            # Defensive: hasIndex already guards row range against
            # rowCount(parent), which equals len(parent_node.children) in
            # our model. Kept as a safety net in case a future override
            # of rowCount diverges from the underlying list — Qt views
            # are notoriously good at finding such mismatches.
            return QModelIndex()
        return self.createIndex(row, column, parent_node.children[row])

    def parent(self, index: _Index = QModelIndex()) -> QModelIndex:  # type: ignore[override]
        """Return the parent index of ``index``, or invalid if at top level.

        Qt's signature takes a :class:`QModelIndex`; we honour the override
        even though the default param matches the base class's no-arg form
        used during initial population.
        """
        if not index.isValid():
            return QModelIndex()
        node = self._node_from_index(index)
        parent_node = node.parent
        if parent_node is None or parent_node is self._root:
            return QModelIndex()
        # Find parent's row in *its* parent (the grandparent).
        grandparent = parent_node.parent
        if grandparent is None:  # pragma: no cover
            # Defensive: every non-root node we construct has a parent
            # back-reference set, so this branch is unreachable in normal
            # flow. Guards against a future tree-builder bug that creates
            # an orphan node — silently returning invalid is safer than
            # a None-deref.
            return QModelIndex()
        row = grandparent.children.index(parent_node)
        return self.createIndex(row, _COLUMN_FIELD, parent_node)

    def rowCount(self, parent: _Index = QModelIndex()) -> int:
        """Number of children of ``parent``. Per Qt convention only column 0
        carries children — column 1 always reports zero."""
        if parent.column() > 0:
            return 0
        return len(self._node(parent).children)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:
        """Always 2 — Field and Value. Constant across the whole tree."""
        del parent  # unused; columnCount is uniform
        return 2

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        """Render the cell at ``index`` for ``role``.

        - ``DisplayRole`` returns the field name (column 0) or value (column 1).
        - ``FontRole`` returns an italic :class:`QFont` for warning/error rows
          so they read as side-info even when the user collapses sections.
        """
        if not index.isValid():
            return None
        node = self._node_from_index(index)
        if role == Qt.ItemDataRole.DisplayRole:
            return node.key if index.column() == _COLUMN_FIELD else node.value
        if role == Qt.ItemDataRole.FontRole and node.italic:
            font = QFont()
            font.setItalic(True)
            return font
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """Header labels for the two columns. Vertical headers (row
        numbers) are suppressed by returning ``None`` for everything
        non-horizontal-display."""
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        return None

    def flags(self, index: _Index) -> Qt.ItemFlag:
        """All cells are selectable + enabled but not editable. Selectable
        is needed for the Phase 5b ``Ctrl+C`` copy-of-selected-value
        affordance."""
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # -- helpers --------------------------------------------------------------

    def _node(self, index: _Index) -> Node:
        """Return the :class:`Node` for ``index`` — the invisible root if
        ``index`` is invalid (top-level case), or the index's internal
        pointer otherwise."""
        if not index.isValid():
            return self._root
        return self._node_from_index(index)

    @staticmethod
    def _node_from_index(index: _Index) -> Node:
        """Type-narrowed alias for ``index.internalPointer()``. Qt's
        ``internalPointer`` returns ``Any``; we know our pointers are
        always :class:`Node` instances."""
        node = index.internalPointer()
        assert isinstance(node, Node)  # noqa: S101 — load-bearing invariant
        return node
