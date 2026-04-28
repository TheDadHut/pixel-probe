# ADR 0002 — Hand-rolled IPTC parser (no `iptcinfo3`)

## Status

Accepted (2026-04-26).

## Context

IPTC IIM metadata in JPEG files is embedded inside Photoshop Image Resource Blocks (IRBs) inside an APP13 segment. The format is a layered byte structure:

1. JPEG segment walker → find APP13 with `Photoshop 3.0\0` signature
2. IRB walker → find resource ID `0x0404`
3. IIM record walker → parse `0x1C` tag-marker records

Pillow's IPTC support is thin; it doesn't expose a full record parser. Our options:

- **Use `iptcinfo3`** — third-party package that does the parsing for us. ~3 dependencies pulled in, last release a few years ago, modest GitHub activity.
- **Hand-roll the parser** — write our own JPEG segment / IRB / IIM walker.
- **Skip IPTC** — drop it from v0.1 scope.

## Decision

Hand-roll the parser in `core/extractors/iptc.py`, with private byte-walking helpers (`_iter_segments`, `_iter_irbs`, `_iter_iim_records`, `_decode`) and a tag table for the common datasets. JPEG-only in v1; TIFF IPTC and PSD are out of scope.

## Consequences

- ✅ Strong portfolio signal: writing a binary-format parser end-to-end is harder than `pip install iptcinfo3`, and reviewing the resulting code shows real engineering depth.
- ✅ Zero additional runtime deps — keeps the dependency graph minimal (Pillow, defusedxml, PySide6 only).
- ✅ Property-based tests via Hypothesis fuzz the byte walkers — exactly the kind of code where Hypothesis earns its keep.
- ✅ We own every byte; bug surface is local.
- ❌ More code to maintain. ~600 lines including tests vs. ~50 lines if we'd used the library.
- ❌ Testing burden falls on us; we need fixture images covering the full record-shape space.
- 🔄 Reconsider if the parser becomes a maintenance burden (unlikely — IPTC IIM is a stable spec frozen in 2009).
