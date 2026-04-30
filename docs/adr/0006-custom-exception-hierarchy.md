# ADR 0006 — Custom exception hierarchy

## Status

Accepted (2026-04-28).

## Context

Extractors hit several distinct catastrophic failure modes:

- The input path doesn't exist or isn't a regular file.
- The file exceeds the configured size cap (DoS gate).
- The file's pixel count exceeds the decompression-bomb threshold.
- The file format isn't supported by the extractor (e.g. IPTC parser receiving a PNG).
- A metadata block is present but malformed (truncated EXIF IFD, broken XMP packet, IPTC IRB with an out-of-bounds size).

We have to choose how callers tell these apart, and how the orchestrator and downstream consumers (CLI, GUI) catch failures uniformly.

Three reasonable approaches were on the table:

- **Bare Python exceptions** — raise `FileNotFoundError`, `ValueError`, `OSError`, etc. Pythonic; zero new types. But callers who want "any extractor failure" need a list of exception types, and that list grows over time as extractors add new failure modes.
- **Single custom error type** — raise `PixelProbeError("missing file: ...")`, distinguish by message string. Catches everything under one clause, but loses the structural information — code that wants to handle "file too large" specifically has to grep the message.
- **Typed hierarchy under one base** — `PixelProbeError` base + named subclasses for each failure mode.

The third gives both properties: callers can `except PixelProbeError` to filter all our errors from third-party noise (Pillow, defusedxml, OS errors), AND code that cares about a specific failure can `except FileTooLargeError` directly. Type-narrowing in handlers works correctly under mypy `--strict`.

## Decision

Define a typed exception hierarchy in `pixel_probe.exceptions`:

- `PixelProbeError(Exception)` — base. Library code never raises bare `Exception` / `ValueError`.
- `MissingFileError` — input path doesn't exist or isn't a regular file.
- `FileTooLargeError` — file exceeds the configured max size.
- `DecompressionBombError` — image pixel count exceeds the configured maximum (wraps Pillow's exception via `__cause__`).

Every **catastrophic, file-level** failure in any extractor must raise a subclass of `PixelProbeError`. The orchestrator's `except Exception` boundary catches them all and converts to error-only `ExtractorResult` so one bad parse doesn't kill the run; downstream code that wants to handle a specific failure can catch the typed subclass before that boundary.

**Partial-extraction failures** (malformed metadata block, format unsupported by a specific extractor, per-tag decode error) are *not* in the hierarchy — they're surfaced as `errors` / `warnings` on the result envelope per [ADR 0003](0003-hybrid-result-shape.md)'s error model. The architecture deliberately keeps these inline so partial diagnostics (e.g., XMP host-format walker warnings) ship alongside the failure rather than being lost when an exception unwinds. See ADR 0003 for the catastrophic-vs-partial distinction.

### Why no `CorruptMetadataError` or `UnsupportedFormatError`

The original draft of this ADR included `CorruptMetadataError` and `UnsupportedFormatError`. Both were defined in `pixel_probe.exceptions` but never raised by any extractor — because [ADR 0003](0003-hybrid-result-shape.md) routes those cases through error-in-result rather than exception propagation. The `except CorruptMetadataError` filtering promise that motivated their inclusion was architecturally unreachable: the orchestrator never sees them.

We removed both classes rather than maintaining defined-but-unraisable types. Adding them back is one-line work if a future code path genuinely needs to raise them; the present architecture doesn't.

## Consequences

- ✅ Single-clause filtering: `except PixelProbeError` catches every extractor failure, distinguishing them from third-party errors that callers shouldn't blanket-suppress.
- ✅ Specific handling stays available: code that legitimately cares about "file too large" vs "bomb" can catch the subclass directly.
- ✅ mypy strict narrows correctly inside `except` blocks — handler code knows it has the specific subclass.
- ✅ Self-documenting: the exception class name is the first line of failure-mode documentation a caller sees.
- ✅ **Honest hierarchy.** Every class in the hierarchy is actually raised somewhere; defined-but-unused types removed.
- ❌ Maintenance discipline: the contract is "every catastrophic path stays inside the hierarchy". This is real work — the initial Phase 1 implementation accidentally raised Python's builtin `FileNotFoundError`, breaking the `except PixelProbeError` filter. The fix was adding `MissingFileError` and updating the call site. The contract needs to be enforced by review, not by tooling.
- ❌ Per-extractor failure modes (corrupt block, unsupported format) aren't typed — callers grep error strings if they want to distinguish them. Acceptable given partial-extraction semantics; revisit if a real consumer needs to filter by failure category.
- 🔄 Reconsider re-introducing typed partial-extraction errors (e.g. `CorruptMetadataError`) if a refactor of the ADR 0003 error model lets exceptions carry partial-extraction context cleanly (e.g., a `partial_data` field). At that point the architectural blocker is gone.
