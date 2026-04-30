# ADR 0011 — Failure-handling normalization across extractors

## Status

Accepted (2026-04-30).

## Context

[ADR 0003](0003-hybrid-result-shape.md), [ADR 0005](0005-sequential-extraction.md), and [ADR 0006](0006-custom-exception-hierarchy.md) jointly defined the project's error model. As the four extractors (file_info, EXIF, IPTC, XMP) shipped, several inconsistencies emerged — not from any one ADR being wrong, but from emergent implementation choices that weren't standardized:

1. **Format-mismatch was a warning in some extractors.** EXIF / IPTC / XMP all returned `data={}, warnings=("File is not a {format}; ...",)` when handed a file the extractor couldn't process at all. But "we couldn't extract anything from this" is a **failure**, not a non-fatal anomaly — the user-visible signal should be an error, not a warning. (file_info is different: it produces partial data — file-level fields ship even for non-images — so its "not an image" warning is correct.)

2. **`data` shape on failure was inconsistent.** Extractors that locally caught their parser errors returned `data={}` (consumer-friendly empty dict). The orchestrator's catch-all returned `data=None`. Same conceptual failure, two shapes depending on which layer caught it.

3. **The orchestrator's catch-all was too broad.** `except Exception` swallowed both expected `PixelProbeError` failures and genuinely unexpected bugs (a `KeyError` from a typo, an `AttributeError` from a misconfigured extractor). Both became indistinguishable error-in-result entries with no traceback for operators to debug from.

[ADR 0006](0006-custom-exception-hierarchy.md)'s "extant-but-corrupt block" cases were also left unaddressed — the architecturally-unraisable `CorruptMetadataError` / `UnsupportedFormatError` classes were removed in [ADR 0012](0012-reduced-exception-hierarchy.md) on the grounds that the orchestrator never sees them.

These weren't blockers for v0.1. But the resulting asymmetry forced consumers to handle each failure mode multiple ways — checking `result.warnings` in some cases, `result.errors` in others; checking `result.data` for both `None` and `{}`. ADR 0011 normalizes the contract.

## Decision

### Failure semantics: `data=None` + error

When an extractor produces **zero data** for an input — because the format is unsupported, the parser failed catastrophically, or some fundamental invariant was violated — the result envelope is:

- `data = None`
- `errors = (...,)` (one or more strings describing the failure)
- `warnings = ()` unless there's an unrelated non-fatal anomaly to surface

This applies uniformly across:

- **Format-mismatch** at the per-extractor level (EXIF: non-image; IPTC: non-JPEG; XMP: non-JPEG/PNG). Was previously a warning with `data={}` — now an error with `data=None`.
- **Whole-file parser failure** (EXIF: `getexif` raises; XMP: `defusedxml` raises `ParseError` or `DefusedXmlException`; XMP: malformed compressed iTXt / decompression bomb / unrecognized compression flag).
- **Catastrophic file-level failures** (`MissingFileError`, `FileTooLargeError`, `DecompressionBombError`) raised by the extractor and caught by the orchestrator.

`data == {}` is reserved for **success-but-empty**: the extractor ran successfully and the file legitimately has no metadata of that kind. Examples:

- A valid JPEG with no EXIF block → `data={}` (EXIF extractor ran, found nothing).
- A valid JPEG with no IPTC IRB → `data={}` (IPTC extractor ran, found nothing).
- A valid JPEG/PNG with no XMP packet → `data={}` (XMP extractor ran, found nothing).

The split is "extractor failed to run" (`data=None`) vs "extractor ran but the file had no relevant data" (`data={}`).

**file_info is the principled exception.** It produces partial data for non-image files: SHA-256, size, and mtime are computed before any image-format check, so `data` is a populated `FileInfo` dataclass with image-level fields as `None`. This is partial extraction (some fields ship, others don't), and the warning surfacing the unrecognized format is the right severity for that case.

### Orchestrator catch tiers

`Analyzer.analyze` distinguishes two exception categories:

1. **`PixelProbeError`** subclasses — expected extractor-side failures. Caught silently and converted to error-in-result with the format `f"{type(e).__name__}: {e}"` (preserves the typed-error class name in the message for debuggability).
2. **Any other `Exception`** — programming bug in the extractor. Logged via `logging.getLogger(__name__).exception(...)` so the traceback is visible to operators, then converted to error-in-result with a `"BUG: "` prefix on the error string. The prefix makes the distinction visible to consumers who want to count "expected failures" vs "actual bugs" across a batch.

`KeyboardInterrupt`, `SystemExit`, and other `BaseException` subclasses still propagate unchanged — that part of [ADR 0005](0005-sequential-extraction.md) is preserved.

### Why error-not-warning for format-mismatch

The old "warning + empty dict" framing was lenient — "you ran me on a non-image, that's odd but not an error." But:

- **The user-visible signal matters.** A consumer iterating `result.errors` to count failures would have missed format-mismatch entirely. The signal-to-noise was wrong: format-mismatch is the loudest "I couldn't help you" message, not a quiet aside.
- **It's symmetric with corrupt-block.** A malformed XMP packet was already an error; "wrong format entirely" is logically more severe, not less.
- **`file_info` stays distinct.** It doesn't violate the new rule: it produces partial data, so warning-with-data is still right for it.

### Why log + bug-prefix for unexpected exceptions

The previous `except Exception` was load-bearing for [ADR 0005](0005-sequential-extraction.md)'s "one bad extractor doesn't kill the rest" guarantee. Narrowing to `except PixelProbeError` would have meant any extractor bug killed the whole `analyze()` call. Two-tier dispatch preserves the resilience while making the bug visible:

- The traceback shows up in stderr / log handlers.
- The `"BUG: "` prefix in the error string lets consumers filter or count separately.
- The result envelope still ships, so other extractors continue.

Operators see what's broken; consumers see the failure; the run completes. All three properties matter.

## Consequences

- ✅ **Uniform failure contract.** `data is None` ↔ failure; `data == {}` ↔ success-but-empty; `result.has_data` (the existing convenience property) does the right thing for both. No more per-extractor variance in failure shape.
- ✅ **Format-mismatch is loud.** Consumers see it on `result.errors` like any other failure. No silent miss.
- ✅ **Bugs are visible.** `logging.exception` surfaces the traceback; `"BUG: "` prefix flags the result for human attention. ADR 0005's resilience guarantee (one bad extractor doesn't kill the rest) preserved.
- ✅ **`PixelProbeError` typed filtering still works** for catastrophic file-level failures. Callers using direct `extractor.extract()` (not via orchestrator) can still `except FileTooLargeError`.
- ❌ **Behavior change for consumers** that assumed `data == {}` after format-mismatch (now `data is None`). Caught by tests; documented here. Acceptable at v0.1-dev.
- ❌ **Two error-string formats.** `"MissingFileError: ..."` (expected) vs `"BUG: KeyError: ..."` (unexpected). Consumers grep for `"BUG:"` if they care about the distinction.
- ❌ **Logging is configured per the consuming application**, not by pixel-probe. A library that doesn't configure logging will see bugs go to the default handler (stderr). Acceptable — that's standard Python library practice.
- 🔄 **Reconsider the `"BUG: "` prefix** if a real consumer wants typed bug filtering. A sentinel attribute on `ExtractorResult` or a separate `bugs: tuple[str, ...]` field would be more rigorous; the prefix is the v0.1-pragmatic choice.
- 🔄 **Reconsider the warning-vs-error split** if a consumer wants to render unsupported-format files differently from corrupt-block failures. Currently both are errors; the type isn't distinguished beyond message content.
