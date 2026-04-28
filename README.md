# pixel-probe

[![CI](https://github.com/TheDadHut/pixel-probe/actions/workflows/ci.yml/badge.svg)](https://github.com/TheDadHut/pixel-probe/actions/workflows/ci.yml)
[![CodeQL](https://github.com/TheDadHut/pixel-probe/actions/workflows/codeql.yml/badge.svg)](https://github.com/TheDadHut/pixel-probe/actions/workflows/codeql.yml)
[![codecov](https://codecov.io/gh/TheDadHut/pixel-probe/branch/main/graph/badge.svg)](https://codecov.io/gh/TheDadHut/pixel-probe)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://github.com/astral-sh/ruff)
[![Type checked: mypy](https://img.shields.io/badge/type%20checked-mypy-1f5082.svg)](https://mypy-lang.org/)

A desktop image-analysis tool built with Python and Qt. Extracts EXIF, IPTC, and XMP metadata, with a custom tree-view UI for inspecting any image at a glance.

## Features

- **EXIF** — camera make/model, exposure, ISO, focal length, GPS as decimal degrees
- **IPTC IIM** — title, keywords, byline, copyright (hand-rolled JPEG APP13/IRB parser; no `iptcinfo3` dep)
- **XMP** — Dublin Core, Photoshop, EXIF, TIFF, IPTC namespaces, parsed XXE-safe via `defusedxml`
- **File info** — format, dimensions, mode, SHA-256, mtime, with decompression-bomb guards
- **CLI + GUI** — same `Analyzer.analyze()` core powers an argparse CLI and a PySide6 desktop app
- 🚧 *Planned:* histogram analysis, dominant-colour extraction, perceptual hashing (lands in `core/analyzers/`)

## Architecture

```
src/pixel_probe/
├── core/
│   ├── analyzer.py              # orchestrator: sequential, constructor DI
│   ├── extractors/              # metadata readers (file_info, exif, iptc, xmp)
│   └── analyzers/               # placeholder — pixel-derived analyses (histogram, etc.)
├── gui/
│   ├── main_window.py           # PySide6 main window
│   ├── widgets/                 # image preview, custom QAbstractItemModel for metadata
│   └── workers/                 # QObject + moveToThread for off-UI-thread analysis
└── cli.py                       # argparse-based CLI
```

Two parallel package families: **`extractors/`** read embedded bytes (EXIF, IPTC, XMP, file metadata — fast, can be empty); future **`analyzers/`** compute from pixels (histogram, dominant colours — slower, always produce output). They share an `Extractor[T]` ABC but live in separate packages because they have genuinely different perf profiles and failure modes.

Architectural decisions are documented as ADRs in [`docs/adr/`](docs/adr/) — five initial decisions covering the extractor/analyzer split, hand-rolled IPTC parsing, the hybrid result shape, constructor DI vs plugin entry points, and sequential-vs-parallel extraction.

## Install

```bash
pip install -e ".[dev]"
pre-commit install
```

To also install Sphinx for the optional docs build:

```bash
pip install -e ".[dev,docs]"
```

## Usage

### CLI

```bash
# Pretty-printed metadata for an image
pixel-probe path/to/image.jpg

# JSON output (pipe-friendly)
pixel-probe path/to/image.jpg --json

# Filter to a single category
pixel-probe path/to/image.jpg --only exif
```

*(CLI lands in PR 4; the entry point is reserved now.)*

### GUI

```bash
python -m pixel_probe.gui.main_window
```

*(GUI lands in PR 5b.)*

## Development

```bash
make install     # pip install -e ".[dev]" + pre-commit install
make test        # pytest
make coverage    # pytest with coverage; HTML report at htmlcov/index.html
make lint        # ruff check + ruff format --check + mypy
make format      # ruff format + ruff check --fix
make run         # launch the GUI
make cli ARGS="path/to/image.jpg --json"
```

### Commit convention

[Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary` — `feat`, `fix`, `refactor`, `test`, `docs`, `chore`. Keeps the git log clean and makes auto-generated release notes possible later.

### Code quality bar

All code is held to the principles documented in `docs/adr/` and reviewed on every PR:

- **OOP fundamentals** — cohesion, encapsulation (private `_helpers`, frozen dataclasses), shallow inheritance hierarchies, polymorphism via the `Extractor[T]` ABC, narrow public APIs
- **SOLID** — Open/Closed (new metadata via new `Extractor` subclass), Liskov, interface segregation, dependency inversion (`Analyzer` takes a list of abstractions)
- **Type safety** — mypy `--strict` + extra error codes (`ignore-without-code` etc.); generic `ExtractorResult[T]`; `Final` on module constants; `tuple[str, ...]` for immutability
- **Determinism** — extractors are idempotent; pure helpers; I/O confined to extractor entry points
- **Fail-loud** — boundary validation; specific `PixelProbeError` subclasses raised at the failure point
- **Security** — `defusedxml` for XMP (XXE-safe); decompression-bomb guards; bounds-checked binary parsing; CodeQL + ruff `S` for SAST

## License

MIT — see [LICENSE](LICENSE).
