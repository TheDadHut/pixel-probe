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

### Three-tier failure classification

Per-extractor results fall into three severity tiers:

| Tier | When | `data` | `errors` | `warnings` |
|---|---|---|---|---|
| **Success-but-empty** | extractor ran, file legitimately has no data of that kind (e.g. JPEG with no EXIF block) | `{}` (or empty payload) | `()` | `()` |
| **Image, format gap** | input *is* a recognized image, just not a format this extractor handles (e.g. IPTC handed a PNG) | `{}` | `()` | `("...unavailable on this format; ...",)` |
| **Failure** | extractor produced zero data because the input wasn't an image at all, or a parser/security gate rejected the bytes | `None` | `("...",)` | `()` unless unrelated anomaly |

The image-format-gap tier exists because pixel-probe is a metadata inspector intended to run across mixed batches (a directory with JPEGs and PNGs and TIFFs). A user running it on a PNG legitimately expects "no IPTC" — surfacing that as an error would drown the actual failures in noise. The shared `is_known_image_format()` helper distinguishes the two cases via byte-prefix signature check.

The split is "extractor failed to run" (`data=None`) vs "extractor ran (or correctly chose not to run) and the file had no relevant data" (`data={}`).

### Failure semantics: `data=None` + error (specifically)

When an extractor produces zero data because the input is **not an image at all** or because a **parser failed catastrophically**, the result envelope is:

- `data = None`
- `errors = (...,)` (one or more strings describing the failure)
- `warnings = ()` unless there's an unrelated non-fatal anomaly to surface

This applies uniformly across:

- **Non-image input** at the per-extractor level (EXIF: `UnidentifiedImageError`; IPTC: bytes not matching any recognized image signature; XMP: same).
- **Whole-file parser failure** (EXIF: `getexif` raises; XMP: `defusedxml` raises `ParseError` or `DefusedXmlException`; XMP: malformed compressed iTXt / decompression bomb / unrecognized compression flag).
- **Catastrophic file-level failures** (`MissingFileError`, `FileTooLargeError`, `DecompressionBombError`) raised by the extractor and caught by the orchestrator.

**file_info is the principled exception** to the three-tier classification. It produces partial data for non-image files: SHA-256, size, and mtime are computed before any image-format check, so `data` is a populated `FileInfo` dataclass with image-level fields as `None`. This is partial extraction (some fields ship, others don't), and the warning surfacing the unrecognized format is the right severity for that case — it's neither a per-extractor format gap nor a complete failure.

### Orchestrator catch tiers

`Analyzer.analyze` distinguishes two exception categories:

1. **`PixelProbeError`** subclasses — expected extractor-side failures. Caught silently and converted to error-in-result with the format `f"{type(e).__name__}: {e}"` (preserves the typed-error class name in the message for debuggability).
2. **Any other `Exception`** — programming bug in the extractor. Logged via `logging.getLogger(__name__).exception(...)` so the traceback is visible to operators, then converted to error-in-result with a `"BUG: "` prefix on the error string. The prefix makes the distinction visible to consumers who want to count "expected failures" vs "actual bugs" across a batch.

`KeyboardInterrupt`, `SystemExit`, and other `BaseException` subclasses still propagate unchanged — that part of [ADR 0005](0005-sequential-extraction.md) is preserved.

### Why split format-mismatch into two tiers

The original draft of this ADR collapsed format-mismatch and non-image into a single "zero data → error" rule. On review, that conflated two distinct cases:

- **Image-but-wrong-format** (PNG → IPTC) — the user's input is a real image, the extractor just doesn't apply. This is a routine outcome of running pixel-probe across a mixed-format directory; surfacing as an error would drown actual failures in noise.
- **Non-image entirely** (text file → IPTC) — the user pointed pixel-probe at something that isn't an image. Real failure; error severity is right.

Distinguishing them costs ~10 lines: a shared `is_known_image_format()` helper that does byte-prefix signature detection, plus a 4-line dispatch in each affected extractor. The output is a meaningfully different UX for legitimate batch workflows.

`file_info` doesn't sit in this classification — it ships partial data for non-images and uses `warnings` to surface the format gap. Documented above as the principled exception.

### Why log + bug-prefix for unexpected exceptions

The previous `except Exception` was load-bearing for [ADR 0005](0005-sequential-extraction.md)'s "one bad extractor doesn't kill the rest" guarantee. Narrowing to `except PixelProbeError` would have meant any extractor bug killed the whole `analyze()` call. Two-tier dispatch preserves the resilience while making the bug visible:

- The traceback shows up in stderr / log handlers.
- The `"BUG: "` prefix in the error string lets consumers filter or count separately.
- The result envelope still ships, so other extractors continue.

Operators see what's broken; consumers see the failure; the run completes. All three properties matter.

## Consequences

- ✅ **Three-tier failure classification** matches the actual UX the tool needs to deliver — success-but-empty stays quiet, image-format-gap surfaces as a warning, true failures surface as errors. Mixed-format batch runs don't drown in false-alarm errors for routine PNG-without-IPTC cases.
- ✅ **Uniform failure-tier shape across extractors.** `data is None` ↔ failure; `data == {}` ↔ either success-but-empty or image-format-gap; `result.has_data` (the existing convenience property) is correct for all three tiers (any of them returns `False` if data is empty/None).
- ✅ **Bugs are visible.** `logging.exception` surfaces the traceback; `"BUG: "` prefix flags the result for human attention. ADR 0005's resilience guarantee (one bad extractor doesn't kill the rest) preserved.
- ✅ **`PixelProbeError` typed filtering still works** for catastrophic file-level failures. Callers using direct `extractor.extract()` (not via orchestrator) can still `except FileTooLargeError`.
- ❌ **Behavior change for consumers** that assumed `data == {}` after non-image input (now `data is None`). Caught by tests; documented here. Acceptable at v0.1-dev.
- ❌ **Two error-string formats.** `"MissingFileError: ..."` (expected) vs `"BUG: KeyError: ..."` (unexpected). Consumers grep for `"BUG:"` if they care about the distinction.
- ❌ **Logging is configured per the consuming application**, not by pixel-probe. A library that doesn't configure logging will see bugs go to the default handler (stderr). Acceptable — that's standard Python library practice.
- ❌ **`is_known_image_format()` is a fixed list of magic bytes.** Niche image formats (JPEG 2000, AVIF, HEIF, etc.) get classified as "non-image" → error rather than warning. Adding to the helper is one-line work; v0.1's list covers the formats users plausibly run pixel-probe against.
- 🔄 **Reconsider the `"BUG: "` prefix** if a real consumer wants typed bug filtering. A sentinel attribute on `ExtractorResult` or a separate `bugs: tuple[str, ...]` field would be more rigorous; the prefix is the v0.1-pragmatic choice.
- 🔄 **Extend `is_known_image_format()`** when a user reports a niche image format being mis-classified as non-image. Append a magic-byte signature to the helper.
