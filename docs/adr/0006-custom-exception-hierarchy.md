# ADR 0006 — Custom exception hierarchy

## Status

Accepted (2026-04-28).

> **Revised by [ADR 0012](0012-reduced-exception-hierarchy.md) (2026-04-30):**
> after Phase 3 review, `CorruptMetadataError` and `UnsupportedFormatError`
> were removed from the implementation — both turned out architecturally
> unraisable given ADR 0003's error model. This ADR is preserved as the
> Phase 1 snapshot; ADR 0012 captures the post-Phase-3 reduced hierarchy.

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
- `UnsupportedFormatError` — extractor doesn't support the given format.
- `CorruptMetadataError` — metadata block exists but cannot be parsed.
- `FileTooLargeError` — file exceeds the configured max size.
- `DecompressionBombError` — image pixel count exceeds the configured maximum (wraps Pillow's exception via `__cause__`).

Every catastrophic failure in any extractor must raise a subclass of `PixelProbeError`. The orchestrator's `except Exception` boundary catches them all and converts to error-only `ExtractorResult` so one bad parse doesn't kill the run; downstream code that wants to handle a specific failure can catch the typed subclass before that boundary.

## Consequences

- ✅ Single-clause filtering: `except PixelProbeError` catches every extractor failure, distinguishing them from third-party errors that callers shouldn't blanket-suppress.
- ✅ Specific handling stays available: code that legitimately cares about "file too large" vs "bomb" can catch the subclass directly.
- ✅ mypy strict narrows correctly inside `except` blocks — handler code knows it has the specific subclass.
- ✅ Self-documenting: the exception class name is the first line of failure-mode documentation a caller sees.
- ❌ Maintenance discipline: the contract is "every catastrophic path stays inside the hierarchy". This is real work — the initial Phase 1 implementation accidentally raised Python's builtin `FileNotFoundError`, breaking the `except PixelProbeError` filter. The fix was adding `MissingFileError` and updating the call site. The contract needs to be enforced by review, not by tooling.
- ❌ Five (and growing) classes is more surface than a single error type would be. Worth it for the type-narrowing win.
- 🔄 Reconsider only if a future failure mode genuinely doesn't fit any existing class and would create a fifth or sixth top-level distinction. Likely never — the five names cover most binary-format failure modes.
