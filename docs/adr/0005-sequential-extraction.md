# ADR 0005 — Sequential extraction (no `ThreadPoolExecutor`)

## Status

Accepted (2026-04-26).

## Context

The `Analyzer` runs N extractors against a single input file. The straightforward implementation is a `for` loop. The "more sophisticated" version is to run them concurrently: submit each `extractor.extract(path)` to a `ThreadPoolExecutor`, pre-populate the result dict to preserve declared order, then join.

Concurrency choices considered:

- **`ThreadPoolExecutor`** — extractors do file I/O, so threads (not processes) are the right primitive; the GIL is fine for I/O-bound work.
- **`asyncio`** — would force every extractor to be `async def`. Adds API friction for no measurable win.
- **`multiprocessing`** — pays a serialization tax for results that are small dicts. Wrong shape.

The case for parallelism is "extractors do I/O". The case against is more interesting.

All four extractors read **the same file**. After extractor #1 has the bytes in memory, the OS page cache holds the file content. Extractors 2–4 are not doing fresh disk I/O — they're reading from RAM via the cache. Threading saves the cost of *cache hits*, which is essentially nothing.

Meanwhile parallelism adds:

- A timing-fragile test (`assert wall_clock < N seconds` that breaks under CI load)
- Thread-safety questions inside each parser (especially the hand-rolled IPTC byte walker)
- Indeterminate ordering in error reports unless we pre-populate the result dict
- Less predictable failure modes (one extractor crashing during `submit` vs `result()`)

## Decision

Sequential iteration. The orchestrator runs extractors in declared order with a plain `for` loop. Per-extractor exceptions are caught and converted to error-only `ExtractorResult` so one bad parse doesn't kill the run.

## Consequences

- ✅ Code is ~30 lines instead of ~80. Easier to read, easier to test.
- ✅ Deterministic ordering — no flaky timing tests.
- ✅ No thread-safety burden inside parsers; the IPTC byte walkers stay genuinely simple.
- ✅ Profiling never showed parallelism would help (page-cache argument above).
- ❌ Wall-clock latency is the sum of all extractor times instead of the max. For four cheap extractors on a small file, this difference is single-digit milliseconds.
- 🔄 Reconsider when a heavy computed *analyzer* lands (perceptual hashing, dominant-colour clustering) that genuinely benefits from off-thread execution. At that point the right design might be: sequential extractors, threaded analyzers — not blanket parallelism.
