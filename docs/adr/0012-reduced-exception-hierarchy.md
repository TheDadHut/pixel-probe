# ADR 0012 — Reduced exception hierarchy

## Status

Accepted (2026-04-30). Revises [ADR 0006](0006-custom-exception-hierarchy.md).

## Context

[ADR 0006](0006-custom-exception-hierarchy.md) (Phase 1, 2026-04-28) defined a five-class typed exception hierarchy under `PixelProbeError`:

- `MissingFileError`
- `UnsupportedFormatError`
- `CorruptMetadataError`
- `FileTooLargeError`
- `DecompressionBombError`

By the end of Phase 3 (after IPTC and XMP shipped), only three of those classes were ever raised by any extractor:

| Class | Raised by |
|---|---|
| `MissingFileError` | EXIF / IPTC / XMP / file_info |
| `FileTooLargeError` | IPTC / XMP / file_info |
| `DecompressionBombError` | file_info |
| `CorruptMetadataError` | (never) |
| `UnsupportedFormatError` | (never) |

The two unused classes' purpose, per ADR 0006, was to let callers `except CorruptMetadataError` to filter "corrupt block" failures specifically, and `except UnsupportedFormatError` for "wrong format" failures. But [ADR 0003](0003-hybrid-result-shape.md)'s error model deliberately routes those cases through **error-in-result** rather than exception propagation:

- EXIF: corrupt block → `return ExtractorResult(..., errors=...)`.
- IPTC: corrupt walker → walker terminates cleanly, partial data ships.
- XMP: malformed XML / failed-decode → `return ExtractorResult(..., errors=...)`.

The orchestrator's catch-all never sees these classes — they're architecturally unreachable. Defined-but-unraisable types are noise.

## Decision

Remove `CorruptMetadataError` and `UnsupportedFormatError` from `pixel_probe.exceptions`. The hierarchy reduces to four classes:

- `PixelProbeError(Exception)` — base.
- `MissingFileError` — input path doesn't exist or isn't a regular file.
- `FileTooLargeError` — file exceeds the configured max size.
- `DecompressionBombError` — image pixel count exceeds the configured maximum (wraps Pillow's exception via `__cause__`).

Every class in the new hierarchy is raised by at least one extractor.

ADR 0006's broader decision — typed hierarchy under one base, `except PixelProbeError` filtering, mypy-strict type narrowing — is **unchanged**. This revision narrows the implementation list to match what actually shipped; it does not reverse the hierarchy approach itself.

## Consequences

- ✅ **Every class in the hierarchy is raised by at least one extractor.** Defined-but-unused types removed.
- ✅ **Smaller public surface.** `pixel_probe.__all__` and `pixel_probe.exceptions.__all__` each shrink by 2 entries.
- ✅ **ADR 0006's "Specific handling stays available" promise is honest now** — it only ever applied to the three raised classes anyway.
- ❌ **Per-extractor failure modes (corrupt block, unsupported format) aren't typed** — callers grep error strings if they want to distinguish them. Acceptable given partial-extraction semantics; revisit if a real consumer needs to filter by failure category.
- ❌ **API breakage** for any consumer that imported `CorruptMetadataError` or `UnsupportedFormatError` from `pixel_probe` or `pixel_probe.exceptions`. v0.1-dev with no documented consumers; acceptable.
- 🔄 **Reconsider re-introducing typed partial-extraction errors** if a refactor of [ADR 0003](0003-hybrid-result-shape.md)'s error model lets exceptions carry partial-extraction context cleanly (e.g., a `partial_data` field on the exception). At that point the architectural blocker is gone and the typed-filtering promise becomes deliverable.
