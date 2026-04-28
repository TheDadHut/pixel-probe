# ADR 0001 — Extractor vs Analyzer split

## Status

Accepted (2026-04-26).

## Context

pixel-probe v0.1 ships metadata extraction (EXIF, IPTC, XMP, file info). The roadmap also includes pixel-derived analyses (histogram, dominant colours, perceptual hashing). Both kinds of capability operate on the same input — an image file — and could plausibly share infrastructure.

Open question at planning time: do they live in one package or two?

The two kinds of work have genuinely different shapes:

- **Metadata extractors** read embedded bytes. They're I/O-bound, fast, and can legitimately produce empty output (no EXIF block in a screenshot is normal, not an error).
- **Pixel-derived analyzers** compute over decoded pixel data. They're CPU-bound, can be slow on large images, and always produce output (you can always compute a histogram).

A unified package would mean the same ABC for both. But "extract" and "analyze" answer different questions ("what's *in* the file?" vs "what does the image *look* like?") and a single name for both would lie.

## Decision

`core/extractors/` and `core/analyzers/` are sibling packages under `core/`, not nested. Each has its own ABC if needed; v0.1 only ships `Extractor[T]`.

`core/analyzers/` is created in PR 0a as an empty package with a docstring explaining its purpose. No analyzer code lands in v0.1 — the directory signals architectural intent.

## Consequences

- ✅ Names match what the things do — readers don't have to inspect the code to learn the semantic difference.
- ✅ The two kinds of work can evolve independently. Adding a `Histogrammer` doesn't touch the extractor ABC.
- ✅ Different perf characteristics stay obvious by directory: `extractors/` is fast; `analyzers/` is the slow path.
- ❌ Two packages instead of one — slight extra navigation cost for someone reading the codebase top-down.
- 🔄 Reconsider if a future capability genuinely fits both categories (e.g., something that reads metadata *and* computes derived signal in one pass). Likely just lives in whichever package matches its primary cost.
