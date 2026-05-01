"""Command-line interface for pixel-probe.

argparse-based front-end on top of :class:`~pixel_probe.core.analyzer.Analyzer` —
the same orchestrator the GUI (Phase 5) drives. Two output modes:

- **text** (default): one section per extractor, key/value lines, warnings
  and errors clearly marked.
- **JSON** (``--json``): the result envelope dumped via ``json.dumps`` for
  pipe-friendly scripting.

Exit codes follow common Unix conventions:

- ``0`` — analysis ran. Per-extractor errors and warnings still surface in
  the output; the caller can grep them. ADR 0011's three-tier classification
  (success-but-empty / image-format-gap / failure) is rendered into the
  text formatter; consumers grep ``error:`` or ``warning:`` markers.
- ``1`` — file-level failure that prevented analysis (missing file).
- ``2`` — argument error. Handled by argparse before reaching ``main``.

The CLI is also reachable as ``python -m pixel_probe.cli`` (the
``__name__ == "__main__"`` guard at the bottom). The ``pixel-probe`` script
entry point in ``pyproject.toml`` calls :func:`main` directly.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterator
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from pixel_probe import __version__
from pixel_probe.core.analyzer import AnalysisResult, Analyzer
from pixel_probe.core.extractors.base import ExtractorResult

__all__ = ["main"]

#: Names accepted by ``--only``. Matches ``Analyzer.default()``'s extractor list.
_KNOWN_EXTRACTORS: tuple[str, ...] = ("file_info", "exif", "iptc", "xmp")


def main(argv: list[str] | None = None) -> int:
    """Run the CLI with the given argv (or ``sys.argv[1:]`` if ``None``).

    Returns the process exit code per the module docstring's contract.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Configure logging to stderr. Argparse's own bad-arg path exits 2 before
    # this is reached, so logging config is set only for the analysis path.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(name)s: %(levelname)s: %(message)s",
    )

    path = Path(args.image)
    if not path.is_file():
        # Pre-flight check before invoking analyze. Two reasons:
        # 1. analyze() catches MissingFileError and converts to error-in-result;
        #    a wall of "MissingFileError" entries from every extractor would
        #    bury the actual problem.
        # 2. Standard CLI behavior: missing file → exit 1, single clear error
        #    on stderr, nothing on stdout.
        print(f"pixel-probe: error: {args.image}: No such file", file=sys.stderr)
        return 1

    if args.only is not None:
        try:
            requested = _parse_only(args.only)
        except ValueError as e:
            print(f"pixel-probe: error: {e}", file=sys.stderr)
            return 2
    else:
        requested = None

    result = Analyzer.default().analyze(path)
    if requested is not None:
        result = _filter_result(result, requested)

    output = _format_json(result) if args.json else _format_text(result)
    print(output)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser. Factored out so tests can drive it
    without invoking ``main`` (and so future contributors don't have to wade
    through ``main`` to find the surface)."""
    parser = argparse.ArgumentParser(
        prog="pixel-probe",
        description="Inspect EXIF, IPTC, and XMP metadata in image files.",
    )
    parser.add_argument("image", metavar="IMAGE", help="Path to an image file")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of formatted text",
    )
    parser.add_argument(
        "--only",
        metavar="CATEGORIES",
        help=(f"Comma-separated extractor names ({','.join(_KNOWN_EXTRACTORS)}). Default: all."),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging to stderr (DEBUG level)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _parse_only(only: str) -> tuple[str, ...]:
    """Parse a comma-separated list of extractor names; validate against the
    known set.

    Whitespace around names is tolerated. Empty tokens (e.g. trailing comma)
    are silently dropped. Unknown names raise ``ValueError`` with a single
    message listing all unknowns — one error message rather than failing on
    the first bad token, so the user fixes everything in one pass.
    """
    requested = tuple(name.strip() for name in only.split(",") if name.strip())
    unknown = [name for name in requested if name not in _KNOWN_EXTRACTORS]
    if unknown:
        msg = (
            f"--only: unknown extractor name(s): {', '.join(unknown)}. "
            f"Valid names: {', '.join(_KNOWN_EXTRACTORS)}"
        )
        raise ValueError(msg)
    return requested


def _filter_result(result: AnalysisResult, names: tuple[str, ...]) -> AnalysisResult:
    """Return a new :class:`AnalysisResult` containing only the requested
    extractors. Names not present in ``result.results`` are silently
    skipped — not an error, since validation against the *known* set
    happened in :func:`_parse_only`; absence here means the caller didn't
    wire that extractor into their analyzer."""
    filtered = {name: result.results[name] for name in names if name in result.results}
    return AnalysisResult(path=result.path, results=filtered)


def _format_json(result: AnalysisResult) -> str:
    """Serialize the result envelope as indented JSON.

    ``asdict`` recurses into nested dataclasses (notably ``FileInfo``);
    tuples become arrays. ``ExtractorResult.data`` can be ``None`` (failure
    per ADR 0011), a dict, or a dataclass — all three serialize correctly.
    ``default=str`` is a safety net for any non-JSON-native value that
    slips through (e.g. a future ``Path`` payload), preventing a crash on
    the way out.
    """
    return json.dumps(asdict(result), indent=2, default=str)


def _format_text(result: AnalysisResult) -> str:
    """Render the result envelope as readable plain text — one ``== name ==``
    section per extractor, then key/value lines, then any warnings or
    errors. Sections appear in the orchestrator's declared order (Python
    dict insertion order); see ADR 0005."""
    lines: list[str] = [f"File: {result.path}", ""]
    for name, entry in result.results.items():
        lines.extend(_format_section(name, entry))
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_section(name: str, entry: ExtractorResult[Any]) -> list[str]:
    """Render one extractor's section.

    Three failure-tier shapes per ADR 0011 are rendered distinctly:

    - ``data is None`` → ``"(no data)"`` placeholder so the section isn't
      blank under a failure header.
    - ``data == {}`` (or empty dataclass-equivalent) → ``"(empty)"``.
    - Populated → key/value lines, with nested dicts flattened via dot-keys
      (e.g. ``gps.latitude``) and lists joined by commas.
    """
    lines: list[str] = [f"== {name} =="]
    data = entry.data
    if data is None:
        lines.append("  (no data)")
    elif is_dataclass(data) and not isinstance(data, type):
        # FileInfo or future dataclass payload. asdict() normalizes to dict.
        as_dict = asdict(data)
        for key, value in _flatten_dict(as_dict):
            lines.append(f"  {key}: {value}")
    elif isinstance(data, dict):
        if not data:
            lines.append("  (empty)")
        else:
            for key, value in _flatten_dict(data):
                lines.append(f"  {key}: {value}")
    else:
        # Defensive: unknown payload shape. Surface as repr so the user
        # sees something rather than a silent blank section.
        lines.append(f"  {data!r}")

    lines.extend(f"  ! warning: {w}" for w in entry.warnings)
    lines.extend(f"  ! error: {e}" for e in entry.errors)
    return lines


def _flatten_dict(d: dict[str, Any], prefix: str = "") -> Iterator[tuple[str, str]]:
    """Walk a (possibly nested) dict; yield ``(key_path, value_str)`` pairs.

    Nested dicts get dot-joined keys (``gps.latitude``); list values are
    comma-joined. Anything else is ``str()``-converted. Recursion is
    finite because EXIF / IPTC / XMP payloads are not self-referential.
    """
    for key, value in d.items():
        key_path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            yield from _flatten_dict(value, key_path)
        elif isinstance(value, list):
            yield key_path, ", ".join(str(v) for v in value)
        else:
            yield key_path, str(value)


if __name__ == "__main__":
    sys.exit(main())
