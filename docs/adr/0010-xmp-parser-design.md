# ADR 0010 — XMP parser design

## Status

Accepted (2026-04-30).

## Context

XMP (Extensible Metadata Platform) is XML data embedded in image files. Unlike EXIF (Pillow-friendly IFD walks) or IPTC (hand-rolled binary parser), XMP is a different shape — a packet of well-formed XML tucked inside a host-format wrapper. Several questions cut across the implementation:

1. **Which XML parser?** stdlib `xml.etree.ElementTree`, `defusedxml`, `lxml`. Each has security and dependency tradeoffs.
2. **Which host formats do we support in v0.1?** XMP can be embedded in JPEG (APP1 segment), PNG (iTXt chunk), TIFF (tag 700), GIF, MP4, PDF, and more. Picking too few leaves gaps; picking too many balloons scope.
3. **How do we flatten RDF into a Python dict?** RDF uses several structured forms (`<rdf:Bag>`, `<rdf:Seq>`, `<rdf:Alt>`, nested `<rdf:Description>`); each needs a flattening rule.
4. **Which namespaces do we surface?** XMP is intentionally extensible — Adobe documents 30+ standard namespaces, vendors define their own. Surfacing all is noise.
5. **Compressed iTXt in PNG.** PNG iTXt chunks can be zlib-compressed. Do we add `zlib` as a runtime dep just for this?
6. **Extended XMP** — XMP-Note specifies an additional packet referenced by `xmpNote:HasExtendedXMP`. Full or partial support?
7. **Malformed XML.** Whether to raise `CorruptMetadataError` or return error-in-result.
8. **XXE** — XMP is XML from arbitrary user-supplied files. The parser is the trust boundary.

## Decision

### `defusedxml` as the XML parser

Use `defusedxml.ElementTree.fromstring`, not the stdlib `xml.etree.ElementTree.fromstring` and not `lxml`.

Considered:

- **stdlib `xml.etree`** — works fine for well-formed input, but the default parser resolves DTDs and external entities. An XMP packet carrying `<!ENTITY xxe SYSTEM "file:///etc/passwd">` would silently leak file contents into the parsed tree.
- **`lxml`** — fast and feature-rich (XPath, XSLT, schema validation). Adds a C-extension runtime dep; we don't need any of its advanced features. Also has its own history of XXE issues that require explicit hardening.
- **`defusedxml`** — pure-Python wrapper that refuses DTDs, entities, and external references by default. API-compatible with stdlib `ElementTree`. Single small Python dep.

`defusedxml` wins on three axes simultaneously: zero C-extension surface, single-line drop-in replacement, security hardening *by default* (not opt-in). The API compatibility means our flattening logic uses stdlib `Element` types — no parser-specific bindings.

### Host-format scope: JPEG APP1 + PNG iTXt only

v0.1 supports XMP in:

- **JPEG** — APP1 segment (`0xFFE1`) whose payload begins with `"http://ns.adobe.com/xap/1.0/\0"`. The dominant carrier in practice; every photo-editing tool writes XMP here.
- **PNG** — uncompressed `iTXt` chunk with keyword `"XML:com.adobe.xmp"`. The standard PNG XMP location.

Out of scope for v0.1:

- **TIFF** (tag 700) — TIFF support overall is deferred until a real-world need surfaces (TIFF EXIF / IPTC are also out of scope).
- **GIF, MP4, PDF, SVG, EPS** — host-format diversity is the long tail; each has its own packet-locator quirks. JPEG + PNG covers ~all of the photo workflows pixel-probe targets.
- **Compressed iTXt** (see below).
- **Extended XMP** (see below).

Per-format dispatch is via signature byte (`_find_xmp_packet` chooses between `_find_xmp_packet_jpeg` and `_find_xmp_packet_png`). Adding TIFF later is one more dispatch arm; the pattern is established.

### RDF flattening: four rules

The parser flattens XMP RDF into a nested `dict[str, dict[str, Any]]` keyed by friendly namespace prefix. Property values follow four rules in `_flatten_value`:

- **`<rdf:Bag>` / `<rdf:Seq>`** of `<rdf:li>` → `list[str]` in document order. (Bag is unordered per spec, Seq ordered; storage layer treats them the same — list naturally preserves order.)
- **`<rdf:Alt>`** of language-tagged `<rdf:li>` → pick `xml:lang="x-default"`, fall back to first `<rdf:li>`. Multilingual property reads as a single string in the consumer's perspective.
- **Nested `<rdf:Description>`** → flattened to a `dict[str, Any]` of its attribute-and-element fields. Used for structured properties like `exif:Flash` (carrying `Fired`/`Mode`/`Function`) and `xmp:Thumbnails`.
- **Anything else** → element's stripped text content (empty string when absent).

Multiple `<rdf:Description>` blocks at the top level merge into the same prefix dict; later wins on duplicates. In practice Description blocks split properties by namespace rather than duplicating fields, so the merge policy rarely fires.

Out of scope: structured values *inside* a Bag/Seq/Alt container (a `<rdf:Description>` wrapped in `<rdf:li>`). The list-style flattening returns `li.text` only, so structured items become empty strings. Real-world XMP rarely uses this combination.

### Friendly-prefix subset

The `_NAMESPACE_PREFIXES` map surfaces six namespaces:

| URI | Prefix |
|---|---|
| `http://purl.org/dc/elements/1.1/` | `dc` |
| `http://ns.adobe.com/xap/1.0/` | `xmp` |
| `http://ns.adobe.com/photoshop/1.0/` | `photoshop` |
| `http://ns.adobe.com/exif/1.0/` | `exif` |
| `http://ns.adobe.com/tiff/1.0/` | `tiff` |
| `http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/` | `Iptc4xmpCore` |

These are the namespaces every photo-editing tool writes and every UI surfaces. Properties from URIs not in this map are dropped from `data` — XMP has a long tail of vendor-specific schemas (camera-raw XMP, Lightroom develop settings, agency-internal taxonomy) that consumers iterating `result.data` would have to filter back out anyway. Same scoping logic as `IPTC_TAGS`.

But silent drops were a footgun: a file with Lightroom-develop XMP looked indistinguishable from a file with no XMP. `_flatten_xmp` now collects the set of dropped namespace URIs and surfaces a single batched warning ("Dropped properties from N unsurfaced namespace(s): URI1, URI2, …") on the result envelope. RDF / XML / xmpNote bookkeeping namespaces are excluded from the count via `_BOOKKEEPING_NAMESPACES` so the warning lists only namespaces that *would* have been surfaced if the friendly map covered them.

The `xmpNote` namespace is detected as a special case for the `HasExtendedXMP` marker but never surfaced in the result.

A parallel **duplicate-field-overwrite warning** fires when two `<rdf:Description>` blocks both define the same `(prefix, field)` pair. Later wins (XMP allows duplicates and we trust the file's order) — but the warning makes the policy visible to consumers who'd otherwise see only one of two definitions with no signal that anything was overridden. Real-world XMP rarely has duplicates (Description blocks split by namespace), so the warning fires only on pathological input.

### Compressed iTXt: bounded decompression via stdlib `zlib`

PNG iTXt chunks have a compression flag (0 = uncompressed, 1 = zlib). Both are supported via the Python standard library — `zlib` is not a runtime-dep cost (we already use it in `scripts/build_fixtures.py` for PNG chunk CRCs).

`_read_itxt_xmp` decompresses flag-1 chunks via `zlib.decompressobj()` with `max_length = _MAX_DECOMPRESSED_XMP_BYTES` (16 MB). The `decompressobj` API + `unconsumed_tail` check is used instead of `zlib.decompress` because:

- **Decompression-bomb DoS defense.** A small compressed iTXt chunk can inflate to gigabytes (zlib achieves easy 1000:1 ratios on repeated bytes). `zlib.decompress` has no output cap and would happily allocate the full payload — same threat surface [ADR 0007](0007-adversarial-input-handling.md) defends against for Pillow's pixel-count gates.
- `decompressobj(...).decompress(data, max_length)` decodes only up to the cap. If `unconsumed_tail` is non-empty afterward, we know the inflated payload exceeds the threshold; we **don't call `flush()`** (which would defeat the cap) and instead reject the chunk as a suspected bomb.
- 16 MB is generous (real-world XMP packets are <100 KB) and bounds adversarial input by ~32:1 against the project-wide `MAX_FILE_SIZE_BYTES` ceiling.

**Failure-case severity.** Three failure modes — malformed `zlib.error`, oversized inflated payload (bomb), and non-spec compression flags — all surface as **errors** (not warnings) on the result envelope. Each is "we found an XMP iTXt chunk and couldn't decode it" — the same severity as the extractor's malformed-XML path. This required plumbing the helper layer to return `(packet, warnings, errors)` 3-tuples instead of `(packet, warnings)` 2-tuples; the extractor merges both into the result.

The original v0.1 framing was "skip + warn" with a "keep zlib decode complexity out of the path" rationale. That framing was wrong on the costs — `zlib` is stdlib, not a dep — and incomplete on the threats — adding decompression without a bomb cap would have been a regression vs. skip-with-warn. The bounded-decompress approach makes the PNG XMP host-format path feature-complete *and* keeps it safe against adversarial input.

### Extended XMP: warn-and-parse-main-only

XMP-Note specifies that a file may carry an additional XMP packet beyond the main one, referenced by an `xmpNote:HasExtendedXMP` attribute or element with a hash pointing to the second packet. The extended packet is typically used for thumbnails, history records, or other large data that wouldn't fit in the JPEG APP1 size limit (~64 KB).

When `_has_extended_xmp` detects the marker, the parser surfaces a warning ("Extended XMP detected; only the main packet is parsed") and continues with the main-packet contents. We don't:

- Locate the extended packet (would require walking additional segments and hash-matching).
- Parse it (full XML parse of a potentially-large secondary packet).
- Merge it into the main packet's tree.

Reasoning, in priority order:

1. **The main packet covers the photo-editor-visible fields** — title, keywords, creator, copyright, headlines. Extended packets carry thumbnails and history, which `_NAMESPACE_PREFIXES` would mostly drop anyway.
2. **Multi-packet support adds plumbing.** Locating + verifying + parsing + merging a second packet is meaningful work; the implementation surface roughly doubles for a feature with marginal real-world value.
3. **The warning makes the limitation visible.** A consumer that sees the warning and needs the extended data knows to look elsewhere.

### Failed-decode paths: error-in-result, not raise

The XMP extractor has four decode-failure paths, all of which surface as **errors** on the result envelope (not raised exceptions, not warnings):

- `defusedxml.DefusedXmlException` — XXE / DTD / external-entity rejection.
- `xml.etree.ElementTree.ParseError` — malformed XML.
- `zlib.error` from `_read_itxt_xmp` — malformed compressed iTXt stream.
- Decompression-bomb cap exceeded — inflated payload would exceed `_MAX_DECOMPRESSED_XMP_BYTES`.
- Non-spec PNG compression flag — chunk shape we can't decode.

All four mean "we found an XMP-shaped chunk but couldn't extract content from it" — same severity tier. Surfacing them uniformly as `errors` lets consumers grep one field for parse failures regardless of which layer (host-format walker vs XML parser) caught them.

The error-in-result approach matches the project-wide pattern established in [ADR 0006](0006-custom-exception-hierarchy.md): catastrophic file-level failures (`MissingFileError`, `FileTooLargeError`, `DecompressionBombError`) raise; partial-extraction issues (corrupt block, unsupported per-extractor format) surface inline on the result envelope so partial diagnostics — e.g. the host-format walker's accumulated warnings — ship alongside the failure rather than being lost when an exception unwinds.

- **EXIF**: a corrupt EXIF block (`Image.getexif` raising) becomes an error string, not a raise.
- **IPTC**: a corrupt IIM record terminates the walker; partial data ships in `data`.
- **XMP**: the four cases above.

The original draft of this ADR flagged `CorruptMetadataError` (then defined but unused) as a separate cleanup question. ADR 0006 has since removed `CorruptMetadataError` and `UnsupportedFormatError` from the hierarchy on the grounds that they were architecturally unreachable — the orchestrator's catch-all never sees them, since extractors deliberately don't propagate "extant-but-corrupt" cases.

### XXE rejection as deliberate portfolio signal

`defusedxml`'s rejection of DTDs / entities / external references isn't an incidental side effect — it's the load-bearing security feature. The flagship test (`test_xxe_payload_blocked` in `tests/test_xmp.py`) feeds an XMP packet containing an external entity reference to a temporary file with a known-secret marker, then asserts:

1. The parse fails with a security exception (rejection works).
2. The secret marker does not appear anywhere on the result envelope (no leak).

If anyone ever swaps `defusedxml.ElementTree.fromstring` for the stdlib `xml.etree.ElementTree.fromstring`, this test fails — either because no error is raised (silently resolved entity) or because the secret leaks into the result. That's the regression guard the security choice depends on.

## Consequences

- ✅ **XXE-safe by default.** No opt-in hardening to forget; the import line is the gate.
- ✅ **Decompression-bomb-safe.** `decompressobj` + `max_length` caps inflated output at 16 MB; suspected bombs are rejected without allocating the full payload. Parallel to ADR 0007's Pillow `MAX_IMAGE_PIXELS` gate.
- ✅ **Drop-in compatibility with stdlib `Element`.** Flattening logic doesn't bind to a specific parser.
- ✅ **Per-format dispatch is small and additive.** Adding TIFF or another host format is a new walker plus a dispatcher arm; no rework of the flattening layer.
- ✅ **Compressed iTXt is supported.** PNG XMP host-format path is feature-complete; failed-decode cases (malformed zlib, bomb-detected, unrecognized flag) surface as errors at the same severity as malformed XML.
- ✅ **Vendor-namespace drops are visible.** A batched warning lists the URIs whose properties were unsurfaced — a user with Lightroom-develop XMP knows the data was present but our friendly map didn't cover it.
- ✅ **Duplicate-field overwrites are visible.** A second batched warning lists the `(prefix, field)` pairs that were silently overwritten by a later `<rdf:Description>`.
- ✅ **Structured properties (`exif:Flash`, etc.) flatten to a dict.** Previously they silently became empty strings; consumers get usable data now.
- ✅ **Friendly-prefix subset surface is reviewable in 6 lines.** Adding a namespace is one entry in the map.
- ❌ **Extended XMP is unsupported.** Files with extended packets see only the main one. Acceptable since main-packet content covers the photo-editor-visible fields.
- ❌ **Structured values inside Bag/Seq/Alt containers flatten to empty strings.** Documented as a v0.1 limitation; recursion into nested-Description-inside-li would double the flattening surface.
- ❌ **Failed-decode paths aren't typed exceptions** — callers grep error-string content to distinguish "XMP parse error" from "Malformed compressed XMP iTXt chunk". Acceptable given the partial-extraction semantic, but a future refactor could let exceptions carry partial context (see ADR 0006's "Reconsider" line).
- 🔄 **Reconsider extended XMP** if a real consumer needs thumbnails-or-history from the second packet. The segment-walker can be extended to locate-and-merge.
- 🔄 **Reconsider TIFF host-format support** when TIFF support overall is added. Tag 700 is the standard location.
- 🔄 **Reconsider the friendly-prefix map** if a real workflow wants Lightroom-develop or another vendor namespace surfaced. Adding a prefix is one map entry; no logic change.
- 🔄 **Reconsider raising `CorruptMetadataError`** as part of a project-wide cleanup of the (currently unused) class — coordinate with the EXIF and IPTC implementations.
