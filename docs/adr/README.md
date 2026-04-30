# Architectural Decision Records

Each architecturally load-bearing decision lives in its own numbered file. Decisions captured here document *why* — the code shows *what*. ADRs are append-only: when a decision is superseded, mark the old one **Superseded by [ADR NNNN](...)** rather than editing it.

Format: [MADR](https://adr.github.io/madr/) (Status / Context / Decision / Consequences).

## Index

- [0001 — Extractor vs Analyzer split](0001-extractor-vs-analyzer-split.md)
- [0002 — Hand-rolled IPTC parser (no `iptcinfo3`)](0002-hand-rolled-iptc-parser.md)
- [0003 — Hybrid result shape (dataclass envelope + dict payload)](0003-hybrid-result-shape.md)
- [0004 — Constructor DI over plugin entry points](0004-constructor-di-over-plugins.md)
- [0005 — Sequential extraction (no `ThreadPoolExecutor`)](0005-sequential-extraction.md)
- [0006 — Custom exception hierarchy](0006-custom-exception-hierarchy.md)
- [0007 — Adversarial input handling](0007-adversarial-input-handling.md)
- [0008 — EXIF parsing strategy](0008-exif-parsing-strategy.md)
- [0010 — XMP parser design](0010-xmp-parser-design.md)

## When to write a new ADR

- The decision changes architecture, not just implementation.
- Reasonable engineers could disagree on the choice.
- A future contributor (including future-you) would ask "why did we do this?"

Don't ADR every commit. Don't ADR things obvious from the code (e.g. "we use pytest"). Do ADR every choice in the "considered and rejected" set on the matrix.

## Template

```markdown
# ADR NNNN — <title>

## Status

Accepted | Proposed | Deprecated | Superseded by [ADR NNNN](...)

## Context

What forces are at play? What's the problem? What did we have to choose between?

## Decision

What did we decide? State it as a present-tense fact.

## Consequences

What changes as a result? List the trade-offs:

- ✅ wins this gives us
- ❌ costs we accept
- 🔄 things we'll reconsider when X happens
```
