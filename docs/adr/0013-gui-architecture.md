# ADR 0013 — GUI architecture

## Status

Accepted (2026-04-30).

## Context

Phase 5 ships a desktop GUI on top of the same `Analyzer` the CLI drives. The GUI's job is narrow — let a user pick an image, see a preview, and inspect the metadata tree — but several decisions cut across it that are not obvious from the code:

1. **How is the metadata tree fed to a `QTreeView`?** Qt offers `QStandardItemModel` (a generic ready-made model) and `QAbstractItemModel` (the abstract base you implement yourself). Either works; the choice affects how testable, performant, and extensible the model is.
2. **Where does the model end and the view begin?** Qt's MV pattern allows the model to depend on the view, the view to depend on the model, both, or neither. The split affects what can be tested without a `QApplication` event loop.
3. **How is `Analyzer.analyze` run off the UI thread?** Qt has two threading idioms — subclass `QThread` and override `run()`, or keep work in a `QObject` and call `moveToThread`. Both compile; one is much harder to get right.
4. **How does the worker hand back its result?** Qt signals across thread boundaries serialize via the metatype system; a custom Python type (`AnalysisResult`) needs `qRegisterMetaType` boilerplate unless we declare the signal as `object`.

These aren't blockers for v0.1 — any choice produces a working GUI — but several of them shape the test architecture and the Phase 6 portfolio narrative. ADR 0013 documents the decisions so the reasoning isn't buried in module docstrings.

## Decision

### Custom `QAbstractItemModel`, not `QStandardItemModel`

`MetadataTreeModel` subclasses `QAbstractItemModel` and implements `index` / `parent` / `rowCount` / `columnCount` / `data` / `headerData` / `flags` directly. ~120 extra lines vs. populating a `QStandardItemModel`.

Considered:

- **`QStandardItemModel`** — Qt's batteries-included model. Build it by `appendRow`-ing `QStandardItem`s. Trivial to populate; comes with editing, drag-and-drop, and persistent indices for free. The cost is *implicit* shape: every row is a `QStandardItem`, every cell has an attached widget-style item, and the model owns those item objects mutably. Read-only contracts are by convention only — nothing stops a future caller from `setData`-ing a row out from under a sort proxy. Font / icon roles are set per-item via `setFont` / `setIcon` rather than computed from the underlying data.
- **Custom `QAbstractItemModel`** — implement the abstract API against a plain Python tree of `Node` dataclasses. Direct control over tree shape, font roles (`FontRole` for italic warning/error rows is computed from `node.italic`, not stamped on a `QStandardItem`), and a clean read-only contract — `set_result` is the *only* mutator and it uses `begin/endResetModel`, so `QSortFilterProxyModel` and any future filter view can wrap the model without surprises.

Custom wins on three fronts that matter here:

1. **Testability.** A `Node` tree is plain Python; the model's `_build_tree` can be exercised without instantiating any widget. With `QStandardItemModel` the tests would be testing Qt's data structure, not ours.
2. **Read-only by construction.** No mutating API exists outside `set_result`. A reviewer can audit "could a row ever change after build?" by reading `__all__`.
3. **Portfolio signal.** Implementing `QAbstractItemModel` correctly — particularly `parent()` and `index()` consistency — is the kind of work that distinguishes a thorough Qt developer from one who only uses convenience widgets.

### Model / view split: model in 5a, view in 5b

`MetadataTreeModel` ships in Phase 5a as standalone code with no view dependency. The matching `MetadataTreeView` (and its host `MainWindow`) ship in Phase 5b. The split is enforced by directory: `gui/widgets/metadata_tree.py` is the model file in 5a, the view is appended in 5b.

Considered:

- **Model + view in one PR.** Smaller PR count, but bundles two distinct concerns into one diff. The tests would have to spin up an event loop just to exercise the tree-building logic.
- **Model + view in one file from the start.** Convenient for reading; couples lifecycle. Once the view imports the model in the same module, tests can't easily import "just the model" without inheriting widget construction.
- **Strict 5a-then-5b sequence.** Model is pure-logic Python (a `QAbstractItemModel` instance is constructible without a `QApplication`; signals fire synchronously). Tests don't need the `gui` pytest marker, run in the default coverage suite, and 5a's PR diff stays focused on logic.

The strict sequence is what we picked. Phase 5b's `MainWindow` imports `MetadataTreeModel`; Phase 5b can't start until 5a lands. Documented in the PR description and module docstring.

### `QObject + moveToThread`, not `QThread` subclass

`AnalysisWorker` is a plain `QObject` with a `run()` slot. The Phase 5b wiring constructs a `QThread`, calls `worker.moveToThread(thread)`, connects `thread.started → worker.run`, and tears down via `worker.finished → thread.quit → deleteLater`.

Considered:

- **Subclass `QThread`, override `run()`.** The classic-but-deprecated idiom. Every Qt tutorial older than 4.8 shows it. The trap: the `QThread` *object itself* lives on the thread that constructed it (typically the UI thread), but `run()` executes on the new thread. Slots on the `QThread` subclass therefore fire on the wrong thread unless connected with `Qt::QueuedConnection` explicitly. Lifecycle bugs (missing `deleteLater`, `quit()` called before `wait()`) are common and silent.
- **`QObject` worker + `moveToThread`.** Recommended by the Qt documentation since 4.8. The worker object's slots fire on the worker thread by default. The `QThread` is just the OS-thread carrier — it has no application logic. Wind-down is signal-driven (`finished → quit → deleteLater`) and uniform across all worker types.

We use the second pattern. The worker carries no `QThread`-specific code; the same class could in principle run synchronously (and does, in `tests/gui/test_analysis_worker.py`, which calls `run()` directly without spinning up a thread).

### `Signal(object)` for `AnalysisResult`, not a registered metatype

`AnalysisWorker.finished` is declared `Signal(object)` rather than `Signal(AnalysisResult)`.

Considered:

- **`Signal(AnalysisResult)`** with `qRegisterMetaType`. Concrete typing on the signal side. Requires calling `qRegisterMetaType("AnalysisResult")` at module import (or before the first cross-thread emit) so Qt can serialize the type for the queued-connection delivery. Boilerplate that has to live somewhere and that breaks if the type is renamed.
- **`Signal(object)`** — Qt's escape hatch for arbitrary Python types. No metatype registration; the signal payload is just a `PyObject*` carried across the thread boundary as-is. Slightly weaker signal-side typing (the slot signature reads `object`), but the receiving slot can immediately annotate it as `AnalysisResult`.

We pick `Signal(object)`. The receiving slot in `MainWindow` will annotate its parameter type explicitly (`def _on_analysis_done(self, result: AnalysisResult) -> None`), so the type information is preserved at the boundary that matters. The win is one less import-time setup step and zero risk of a forgotten `qRegisterMetaType` call hanging on type rename. `Signal(str)` for `failed` follows the same logic — strings don't need registration but `object` would be wrong (over-broad).

## Consequences

- ✅ **Model is unit-testable without an event loop.** `MetadataTreeModel` is a `QAbstractItemModel` instance, but its construction + tree-building logic exercises pure Python. The 5a test suite has 24 tests at 100% coverage and runs under the default (non-`gui`) pytest marker.
- ✅ **`QSortFilterProxyModel` wrap-safety.** `set_result` is the only mutator and uses `begin/endResetModel`. No row insertion or removal signals fire outside the reset pair. A future "filter EXIF tags by name" affordance can wrap the model in a proxy without re-implementing.
- ✅ **Worker lifecycle is signal-driven and uniform.** `worker.finished → thread.quit → thread.finished → deleteLater(worker, thread)`. No manual `wait()` calls, no slot-on-wrong-thread footguns.
- ✅ **Synchronous test path.** The worker's `run()` is callable directly; tests assert signal contracts via `signal.connect(list.append)` capture, no `qtbot.waitSignal`, no event loop. Fast tests, no `gui` marker pollution.
- ✅ **Read-only contract by construction.** No mutating API exists on `MetadataTreeModel` outside `set_result`. Reviewer-friendly and proxy-friendly.
- ❌ **~120 extra lines of model plumbing** vs. `QStandardItemModel`. Justified by the testability + read-only-contract + portfolio benefits above. Documented in the model's module docstring so a maintainer doesn't decide "let's just use QStandardItemModel" without reading the why.
- ❌ **Signal payload typing is `object`, not `AnalysisResult`.** Slot side has to re-narrow. Low cost in practice; PySide6 type stubs surface the error if a slot mis-annotates.
- ❌ **`Node` is mutated during `_build_tree`** (the `parent` and `row_in_parent` back-pointers are stamped after construction). The `_append_child` helper centralizes the mutation; outside `_build_tree` the tree is effectively immutable.
- 🔄 **Reconsider `Signal(AnalysisResult)` + `qRegisterMetaType`** if a future reviewer wants signal-side type assertions visible in IDE tooling. The change is local to the worker module.
- 🔄 **Reconsider model split into `Model` + `Node` files** if `metadata_tree.py` grows past ~400 lines. Today it's ~310; the dataclass + model fit comfortably together.
- 🔄 **Reconsider the move to `QStandardItemModel`** only if a future feature needs editing or drag-drop and the read-only contract becomes a fight. Unlikely for an inspector tool.
