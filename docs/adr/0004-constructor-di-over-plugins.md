# ADR 0004 — Constructor DI over plugin entry points

## Status

Accepted (2026-04-26).

## Context

The `Analyzer` orchestrator runs a list of extractors. How is that list assembled, and how do we make it extensible?

Two reasonable choices:

- **Constructor-based DI** — `Analyzer(extractors=[...])`, with a `default()` factory that returns the built-in set. Anyone wanting a different set instantiates with their own list.
- **Entry-point plugin discovery** — declare a `pixel_probe.extractors` group in `pyproject.toml`. Built-ins register there; third parties can ship a separate package that declares the same group, and `Analyzer.default()` discovers everyone via `importlib.metadata.entry_points()`.

Plugin discovery is a real architecture pattern with a real cost surface: an entry-point registration block, name-collision rules (built-ins win), `hiddenimports` for any future binary build (PyInstaller's static analysis can't follow dynamic plugin loads), and a docstring contract for what a third-party extractor must implement.

## Decision

Constructor-based DI. No plugin discovery machinery. `Analyzer.default()` returns the built-in extractor list directly, hard-coded.

## Consequences

- ✅ Zero machinery for entry-point registration, collision rules, or distribution-time hidden-import maintenance.
- ✅ The extension story is "build your own `Analyzer` with your own list" — that *is* perfect dependency injection. Any third party that wants to extend pixel-probe imports `Extractor`, subclasses it, and instantiates an `Analyzer` with their list.
- ✅ Code stays simpler. No "what if a plugin defines an extractor named `file_info`" branching.
- ❌ A third party who wanted to ship `pixel-probe-raw` or `pixel-probe-heic` as an installable side-package would need to publish a constructor wrapper, not just register an entry point. Slightly more friction for them.
- 🔄 Reconsider when an external party actually wants to ship an extractor as a separate pip-installable package. Until that demand exists, plugin entry points are YAGNI complexity for zero current users.
