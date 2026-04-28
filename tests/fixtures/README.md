# Test fixtures

This directory holds image fixtures used by the test suite.

Fixtures fall into two categories:

- **Generated** — produced by `scripts/build_fixtures.py` (Pillow + `piexif`) for deterministic content with known SHA-256 and controlled metadata. Suitable for hash-based assertions and tag-specific tests.
- **Real-world** — small images sourced from public-domain or BSD-licensed corpora (e.g., ExifTool sample images), kept under ~5 KB each via aggressive recompression. Used for integration tests where realism matters more than determinism.

Each fixture below documents: filename, content summary, origin/license, and expected SHA-256 where applicable.

## Index

_Populated as fixtures land in their respective phases. See `PLAN.md` PR breakdown for the per-phase fixture list._

| Fixture | Phase | Source | Contents | SHA-256 |
|---|---|---|---|---|
| _none yet (Phase 0)_ | | | | |
