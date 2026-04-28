# ADR 0007 — Adversarial input handling

## Status

Accepted (2026-04-28).

## Context

pixel-probe parses binary metadata from user-supplied images. That puts us in the same threat surface as Pillow itself, which has had decompression-bomb CVEs (notably CVE-2014-3589 and several others). Three concrete risks:

1. **File-size DoS** — a user opens a 50 GB "image" (deliberately crafted, or wrong file type). Reading the whole thing into memory or spending minutes on `Image.open` is a real failure mode.
2. **Decompression bomb** — an image with a small compressed file size but a massive pixel count (millions × millions). Pillow ships a `MAX_IMAGE_PIXELS` global with a default around 178 megapixels, and it RAISES `DecompressionBombError` only when pixels exceed `2 × MAX_IMAGE_PIXELS`. Below that, it merely emits a `DecompressionBombWarning`. So a 178 MP attack image triggers a warning, not an error, on default Pillow settings.
3. **Concurrent state corruption** — `Image.MAX_IMAGE_PIXELS` is a process-global. If two extract calls overlap (Phase 5's GUI worker thread plus a future second call), each saves the threshold, sets its own value, and restores. Without serialization, Thread A can save Thread B's modified value and "restore" the wrong threshold.

We need to address all three before any concrete extractor ships.

## Decision

Three coordinated mechanisms in `pixel_probe.core.extractors.file_info`, each load-bearing on its own:

1. **`MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024`** (500 MB). Checked via `path.stat()` before any read. Files larger than this raise `FileTooLargeError` immediately — no Pillow allocation, no SHA-256 streaming, no header parse.
2. **`MAX_IMAGE_PIXELS = 100_000_000`** (100 MP — tighter than Pillow's default ~178 MP). Inside `extract`, save Pillow's current `MAX_IMAGE_PIXELS`, set it to ours, and **escalate `DecompressionBombWarning` to an exception via `warnings.filterwarnings("error", ...)`**. The catch block converts both `Image.DecompressionBombWarning` and `Image.DecompressionBombError` into our `DecompressionBombError`, with the original chained as `__cause__`. Net effect: bombs are caught at 1× threshold, not 2× (Pillow's default behavior).
3. **`_PILLOW_MAX_PIXELS_LOCK = threading.Lock()`** module-level. The save/restore block runs inside `with _PILLOW_MAX_PIXELS_LOCK:`. Concurrent `extract` calls — most importantly from Phase 5's GUI `AnalysisWorker` thread — serialize through the lock so neither can observe the other's stale state.

All three are enforced *before* the file body is read into Pillow. SHA-256 streaming runs separately (it's safe regardless of pixel count), so we still produce the file-level fields (size, hash, mtime) for files that fail the image-format checks.

## Consequences

- ✅ **Defense in depth at three layers**: pre-read size gate, pixel-count gate at 1× (not Pillow's default 2×), and concurrency gate on the global state.
- ✅ **Phase 5 readiness**: the GUI worker can safely call `Analyzer.analyze` from a background thread without coordinating with other potential callers. The lock handles it.
- ✅ **Typed errors with chained causes**: `DecompressionBombError("...")` from `Image.DecompressionBombWarning("...")` lets debuggers see the original Pillow message. Callers `except DecompressionBombError` to handle the family.
- ✅ **Tunable**: both `MAX_FILE_SIZE_BYTES` and `MAX_IMAGE_PIXELS` are `Final` module constants. A future config layer can override them; `pytest`'s `monkeypatch.setattr` covers them in the test suite (used by `test_oversized_file_raises_file_too_large` and `test_decompression_bomb_chains_pillow_error`).
- ❌ **Rejecting potentially-malicious input means occasionally rejecting legitimate edge cases.** A real 12000 × 9000 pixel photo (108 MP) trips our 100 MP bar even though it's not adversarial. Acceptable for a desktop-inspector use case; a server context would warrant raising the cap.
- ❌ **Lock cost**: the lock serializes all `file_info.extract` calls across threads. For our scale (a single user clicking through images in a GUI) this is invisible. A multi-tenant server context would want per-thread Pillow contexts instead, but we're not that.
- ❌ **`warnings.filterwarnings("error", ...)` inside the lock changes process state for the duration**. The `with warnings.catch_warnings()` scope undoes it on exit, but a concurrent caller in another thread that runs *outside* this lock would briefly see the escalated filter. Not a correctness issue (Pillow's `MAX_IMAGE_PIXELS` is the real gate) but worth noting.
- 🔄 **Reconsider thresholds when a real user hits one**. If a legitimate workflow needs a 200 MP image, raise the cap rather than removing the gate. Removing the gate is the wrong move — it's the security layer.
- 🔄 **Reconsider the lock if Pillow ever adopts a thread-local `MAX_IMAGE_PIXELS`**. Then the global save/restore is no longer racy and the lock becomes redundant.
