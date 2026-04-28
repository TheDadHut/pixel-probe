# Test fixtures

This directory holds image fixtures used by the test suite.

Fixtures fall into two categories:

- **Generated** — produced by `scripts/build_fixtures.py` (Pillow-based) for deterministic content with known SHA-256 and controlled metadata. Suitable for hash-based assertions and tag-specific tests.
- **Real-world** — small images sourced from public-domain or BSD-licensed corpora (e.g., ExifTool sample images), kept under ~5 KB each via aggressive recompression. Used for integration tests where realism matters more than determinism.

Each fixture below documents: filename, content summary, origin/license, and expected SHA-256 where applicable.

## Index

| Fixture | Phase | Source | Contents | SHA-256 |
|---|---|---|---|---|
| `tiny.jpg` | 1 | Generated (`scripts/build_fixtures.py`) | 1×1 black JPEG, ~285 bytes | `d02ca6bbf6a4d459053b6c2f670ceb8e5d0049cedb1ca9195ff13fa953861f69` |
| `tiny.png` | 1 | Generated (`scripts/build_fixtures.py`) | 1×1 transparent PNG, ~70 bytes | `f2bb5bbaca678ecad746b1fa5ecfa2c8a81dd18817be19f0187c036d25326317` |
| `not_an_image.txt` | 1 | Generated (`scripts/build_fixtures.py`) | Plaintext "This is not an image file." | `a363aeea914f9e13d4e6ab5edfe6ccf736514ac9af2e290f336fd1202cb115d2` |

## Regenerating

```bash
make fixtures
```

Re-run after a Pillow major-version bump if `tests/test_file_info.py` SHA assertions start failing.
