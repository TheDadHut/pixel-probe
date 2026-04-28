"""CLI entry point.

Real implementation lands in PR 4 — this is a stub so the package installs cleanly
and the ``pixel-probe`` script is wired up.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Stub CLI. Phase 4 replaces this with argparse + extractor wiring.

    The ``argv`` parameter shape is the documented future API — Phase 4 will use it
    to make the CLI testable via subprocess-free in-process invocation.
    """
    print("pixel-probe CLI is not yet implemented. See PLAN.md PR 4.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
