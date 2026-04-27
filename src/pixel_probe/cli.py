"""CLI entry point.

Real implementation lands in PR 4 — this is a stub so the package installs cleanly
and the ``pixel-probe`` script is wired up.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Stub CLI. Phase 4 replaces this with argparse + extractor wiring."""
    del argv  # unused until PR 4
    print("pixel-probe CLI is not yet implemented. See PLAN.md PR 4.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
