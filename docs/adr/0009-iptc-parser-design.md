# ADR 0009 — IPTC parser design

## Status

Accepted (2026-04-30).

## Context

[ADR 0002](0002-hand-rolled-iptc-parser.md) already settled the *whether* — we hand-roll the parser instead of pulling `iptcinfo3`. This ADR captures the *how*: the design choices inside `pixel_probe.core.extractors.iptc` that reasonable engineers could disagree on.

IPTC IIM in JPEG is a layered byte structure with three nested levels:

1. **JPEG segments** — the `0xFF<marker> <length> <payload>` sequence between SOI and SOS.
2. **Photoshop Image Resource Blocks (IRBs)** inside the APP13 segment that begins with the `Photoshop 3.0\0` signature.
3. **IIM records** (`0x1C` tag-marker, record-number, dataset-number, length, value) inside the IRB whose resource ID is `0x0404`.

Several design questions cut across the implementation:

- How do we decompose the parser? One mega-walker, or one walker per layer?
- What happens when bytes are malformed? Raise an exception, return partial data, or skip silently?
- How do we handle `(1, 90) CodedCharacterSet`, which can appear in any record position but governs how to decode every other text record?
- How big should the surfaced tag table be? IIM has hundreds of obscure datasets.
- What about extended-size records (high bit in the size field) — partial support, fail-stop, or fail-soft?
- How do repeatable datasets (Keywords) interact with the result-payload type?

The choices below all have credible alternatives.

## Decision

### Three-walker decomposition

The parser is three private generator functions, each operating on the layer above:

- `_iter_segments(data: bytes)` yields `(marker, payload)` for each JPEG segment.
- `_iter_irbs(payload: bytes)` yields `(resource_id, data_block)` for each IRB inside the APP13 payload.
- `_iter_iim_records(block: bytes)` yields `(record, dataset, value_bytes)` for each IIM record inside the IPTC IRB.

`IptcExtractor.extract()` is orchestration only — it calls the walkers via two static helpers (`_find_irb_payload`, `_find_iim_block`), runs the two-pass charset resolution (see below), and applies the tag table.

The alternative was one combined walker that fused all three layers into a single state machine. Three layers map to the format's nesting and let each level be unit-tested in isolation; the cohesion / separation-of-concerns wins outweigh the modest extra surface area.

### Never-raise invariant; corruption is fail-soft, not catastrophic

Every walker returns `Iterator[...]` and **never raises** on any byte sequence. Truncated lengths, bad signatures, declared sizes that walk past end-of-buffer, and extended-size records all terminate the iteration cleanly. The walker yields whatever was successfully parsed before the corruption boundary; the extractor surfaces the partial result.

This is enforced by a Hypothesis property test per walker over arbitrary 0–1024 byte inputs (`tests/property/test_iptc_walkers.py`), plus hand-written tests for each specific corruption shape.

The alternative was raising `CorruptMetadataError` on the first malformed read. Fail-soft was chosen because:

- IPTC corruption in real-world files is overwhelmingly *partial* — one bad record amid otherwise-valid metadata. Raising would lose every successfully-parsed record before it.
- The result envelope ([ADR 0003](0003-hybrid-result-shape.md)) already supports partial-data semantics; using it lets downstream renderers (CLI, GUI tree) display what's there without special-casing failures.
- Walker termination is local — a corrupt IRB doesn't take down sibling segments; a corrupt IIM record doesn't take down sibling IRBs.

`MissingFileError` and `FileTooLargeError` *are* raised — they're fail-stop conditions that happen before any byte walking begins. Inside the walkers, every offset read is bounds-checked.

### Two-pass charset resolution

`(1, 90) CodedCharacterSet` is an ISO 2022 escape sequence that selects how every text record should be decoded. The IIM spec doesn't constrain its position within the record stream — it can appear anywhere.

The parser materializes the IIM records into a list, finds the charset record (if any) in a pre-pass, then iterates the records again with the resolved codec. The charset record itself is consumed for decode selection and **not** surfaced in the public payload — it's an implementation detail.

The alternative was a single pass that assumed `(1, 90)` would always come first. This would be correct for most real-world files (most encoders put it at the top) but would silently misdecode when it doesn't. Two passes over a typically-small IRB block is cheap; correctness beats the micro-optimization.

### `IPTC_TAGS` is a deliberate subset

The surfaced tag table is twelve datasets covering Title, Keywords, DateCreated, Byline, BylineTitle, City, ProvinceState, Country, Headline, Copyright, Caption, CaptionWriter — the photo-editor-visible fields users actually consume.

The IIM spec defines hundreds of datasets, the bulk of which are obsolete (ARPA-era news-wire fields), photo-agency-internal, or carry binary blobs nobody renders. Surfacing all of them as `Tag_X_Y` would be noise; consumers iterating `result.data` to render "IPTC info" would have to filter the table back down to the same subset anyway.

Datasets not in the table are silently dropped — no warning, no error. The contract is "we surface useful subset; the rest is intentionally not parsed."

### Extended-size IIM records: deliberate fail-stop

When a record's size field has the high bit set, the IIM spec defines the lower 15 bits as a *length-of-length* — i.e., "the next N bytes contain the real size", letting values exceed the 32 KB limit of a normal 2-byte size.

Decoding extended records mechanically is straightforward — maybe 8 lines in `_iter_iim_records` to read the length-of-length field, then the actual size, then the value. We don't do that. The walker terminates on the first extended record; subsequent records in the same IRB are not parsed. The reasoning, in priority order:

1. **They're vanishingly rare in real files.** The 0x8000 size flag was specced for embedded raw audio/video as IIM records — a use case that never caught on. Photoshop, Lightroom, Camera Raw, exiftool, and digiKam don't emit them for photo metadata. The chance a v0.1 user feeds us a file with one is effectively zero.
2. **Their values would almost always be binary blobs.** Photo-editor-visible metadata (the `IPTC_TAGS` subset) is universally text-encoded. Supporting extended records meaningfully would mean porting [ADR 0008](0008-exif-parsing-strategy.md)'s bytes-summarization gate (`<binary, N bytes>`) to IPTC — adding complexity for a tag table whose values are otherwise always strings.
3. **Fail-stop is safe-but-lossy, not incorrect.** The walker doesn't *misparse* on encountering one — it stops. Whatever was parsed before ships in `data`. A pathological file with an extended record at position 0 would yield empty `data`, but it wouldn't yield wrong `data`. Silently advancing past the extended record by guessing its length-of-length encoding (the only option short of full support) would risk misparsing the rest of the IRB and surfacing fabricated tag values — strictly worse.

The cost of full support is real (parser code + tests + bytes-summarization plumbing); the value for our scope is zero. Fail-stop is the deliberate choice.

### Repeatable tags and the disjoint-name invariant

Some IIM datasets repeat (Keywords being the canonical example — multiple `(2, 25)` records in one file). They accumulate into `list[str]` in the result. Scalar datasets overwrite.

Two tables drive the dispatch:

- `IPTC_TAGS: dict[tuple[int, int], str]` — the (record, dataset) → friendly-name table.
- `_REPEATABLE_TAGS: frozenset[tuple[int, int]]` — the subset of `IPTC_TAGS` keys that may repeat.

The result type is `IptcData = dict[str, str | list[str]]` (matches [ADR 0003](0003-hybrid-result-shape.md)). For correctness, repeatable and scalar tags must have **disjoint friendly names** — if a name appeared in both, dispatch could overwrite a list with a string, or try to `.append` to a string. A regression test (`test_repeatable_and_scalar_tags_have_disjoint_names`) enforces the invariant at the table-design layer.

The alternative was making *every* value `list[str]` (single-item lists for scalars). It's simpler dispatch but uglier consumer code — every reader would unwrap one-element lists. Keeping scalar values as bare strings matches how every other IPTC tool surfaces them.

### `errors="replace"` decode policy

`_decode` uses `value.decode(codec, errors="replace")`. A single bad byte in one tag never kills the rest of the parse — it becomes the Unicode replacement character (U+FFFD) and the rest of the value continues.

The alternative was `errors="strict"` (raise on bad bytes) or `errors="ignore"` (drop bad bytes silently). IPTC text in the wild is sometimes double-encoded or contains stray bytes; replacement is the most useful failure mode — visible to consumers, not crashing, not silently truncating. Defensive `LookupError` fallback in `_decode` handles unknown codec names.

## Consequences

- ✅ **Each layer is independently unit-testable.** `_iter_segments` / `_iter_irbs` / `_iter_iim_records` are pure functions of bytes; their unit tests don't need fixtures, Pillow, or filesystem I/O. Hypothesis fuzzes each walker for the never-raise property.
- ✅ **Adversarial input is contained.** Bounds checks at every offset read; no walker can be tricked into out-of-bounds reads or infinite loops. Reinforces [ADR 0007](0007-adversarial-input-handling.md)'s defense-in-depth posture for binary parsers (which 0007 didn't cover — it was Pillow-specific).
- ✅ **Charset resolution is order-agnostic.** Files with the charset escape mid-stream (or absent entirely) are handled correctly, not just the common encoder shape.
- ✅ **The tag table is reviewable in 15 lines.** Adding a dataset is changing one line in `IPTC_TAGS`, plus adding to `_REPEATABLE_TAGS` if it repeats — the disjointness test catches name collisions.
- ❌ **Fail-soft on corruption hides parse failures from consumers who'd want to know.** A walker that stops at byte 800 returns the same result shape as one that walked the whole IRB cleanly. Acceptable trade-off — a future caller who needs strict parsing can detect this by counting expected vs received records, but the common case (display whatever metadata is there) wins.
- ❌ **Extended-size records terminate the walk early.** A file with an extended record at position 0 would yield empty `data`. Acceptable because (1) extended records are essentially absent from real-world IPTC, (2) any meaningful support would require porting bytes-summarization plumbing for the binary blobs they'd carry, and (3) silently advancing past one by guessing its length-of-length encoding would risk surfacing fabricated tag values — strictly worse than fail-stop.
- ❌ **Datasets outside `IPTC_TAGS` are dropped silently with no diagnostic.** A user who expects an obscure tag to surface won't get a "we don't parse this" warning. Acceptable for v0.1 — adding the warning is one line if it ever becomes a real complaint.
- 🔄 **Reconsider the never-raise invariant** if a downstream consumer (CLI, GUI) actually needs to distinguish "fully parsed" from "stopped at corruption". The walker could yield a sentinel or expose a "terminated_early" flag without changing the no-raise property.
- 🔄 **Reconsider the tag table size** if real users complain about a missing common field. The 12-dataset list was sized for "what photo-editor UIs surface" and could grow if a use case demands it.
- 🔄 **Reconsider extended-record support** if a real workflow surfaces a file whose interesting metadata lives past an extended record. The walker change is ~8 lines; the larger lift is wiring in bytes-summarization for the multi-MB blobs they'd carry. Cheap to do once there's a real use case to justify it.
- 🔄 **Reconsider the two-pass charset** if profiling ever shows the materialization is a hot path. Single-pass with deferred decode (collect raw values, decode at the end) is a possible alternative, but unlikely to matter — IRBs are <4 KB in practice.
