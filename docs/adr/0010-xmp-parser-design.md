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

These are the namespaces every photo-editing tool writes and every UI surfaces. Properties from URIs not in this map are silently dropped — XMP has a long tail of vendor-specific schemas (camera-raw XMP, Lightroom develop settings, agency-internal taxonomy) that consumers iterating `result.data` would have to filter back out anyway. Same scoping logic as `IPTC_TAGS`.

The `xmpNote` namespace is detected as a special case for the `HasExtendedXMP` marker but never surfaced in the result.

### Compressed iTXt: skip + warn

PNG iTXt chunks have a compression flag (0 = uncompressed, 1 = zlib). v0.1 supports only uncompressed iTXt. Compressed XMP-iTXt is rare in practice — most PNG encoders write uncompressed for compatibility — but the warning matters because a silently-skipped compressed chunk would look indistinguishable from "no XMP" to the user.

The compression check uses `flag != 0` (not `flag == 1`), treating any non-zero value as "skip + warn" rather than reading the rest as text. Robust against malformed chunks where the flag is some other non-spec value.

The alternative was carrying `zlib` (stdlib, free) and decompressing transparently. Rejected because the user-visible payoff — supporting an unusual encoding choice for an uncommon host-format/metadata combination — doesn't justify the additional decode-failure surface area (corrupt zlib stream → another error path to think about). The skip-with-warning approach surfaces the issue to the user rather than masking it.

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

### Malformed XML: error-in-result, not raise

When `defusedxml.ElementTree.fromstring` raises `xml.etree.ElementTree.ParseError` (malformed XML) or `defusedxml.DefusedXmlException` (security rejection), `extract` catches both and returns an `ExtractorResult` with an error string in the `errors` tuple — *not* a raised `CorruptMetadataError`.

This deviates from a strict reading of [ADR 0006](0006-custom-exception-hierarchy.md), which describes `CorruptMetadataError` as the right class for "metadata block exists but cannot be parsed". The implementation chose error-in-result for consistency with the EXIF and IPTC patterns:

- **EXIF**: a corrupt EXIF block (`Image.getexif` raising) becomes an error string, not a raise.
- **IPTC**: a corrupt IIM record terminates the walker; partial data ships in `data`.

Across the three extractors, "extant-but-corrupt block" never raises out of `extract`. Catastrophic failures that *do* raise are file-level: `MissingFileError`, `FileTooLargeError`, `DecompressionBombError`. The `CorruptMetadataError` class is defined but unused project-wide. A future cleanup could either start using it consistently or remove it.

### XXE rejection as deliberate portfolio signal

`defusedxml`'s rejection of DTDs / entities / external references isn't an incidental side effect — it's the load-bearing security feature. The flagship test (`test_xxe_payload_blocked` in `tests/test_xmp.py`) feeds an XMP packet containing an external entity reference to a temporary file with a known-secret marker, then asserts:

1. The parse fails with a security exception (rejection works).
2. The secret marker does not appear anywhere on the result envelope (no leak).

If anyone ever swaps `defusedxml.ElementTree.fromstring` for the stdlib `xml.etree.ElementTree.fromstring`, this test fails — either because no error is raised (silently resolved entity) or because the secret leaks into the result. That's the regression guard the security choice depends on.

## Consequences

- ✅ **XXE-safe by default.** No opt-in hardening to forget; the import line is the gate.
- ✅ **Drop-in compatibility with stdlib `Element`.** Flattening logic doesn't bind to a specific parser.
- ✅ **Per-format dispatch is small and additive.** Adding TIFF or another host format is a new walker plus a dispatcher arm; no rework of the flattening layer.
- ✅ **Friendly-prefix subset surface is reviewable in 6 lines.** Adding a namespace is one entry in the map.
- ✅ **Compressed-iTXt warning prevents silent misses.** A user with a compressed-XMP PNG sees "Compressed XMP iTXt chunk detected" rather than "no XMP".
- ✅ **Structured properties (`exif:Flash`, etc.) flatten to a dict.** Previously they silently became empty strings; consumers get usable data now.
- ❌ **Vendor-namespace properties are dropped silently with no diagnostic.** A user expecting Lightroom-develop XMP fields won't get a "we don't surface this" warning. Acceptable for v0.1; the alternative (surface as `nsN_field`) was worse for downstream consumers.
- ❌ **Compressed iTXt is unsupported.** A PNG with compressed XMP returns empty data + a warning instead of decompressing. Trade-off for keeping zlib decode complexity out of the path.
- ❌ **Extended XMP is unsupported.** Files with extended packets see only the main one. Acceptable since main-packet content covers the photo-editor-visible fields.
- ❌ **Structured values inside Bag/Seq/Alt containers flatten to empty strings.** Documented as a v0.1 limitation; recursion into nested-Description-inside-li would double the flattening surface.
- ❌ **Malformed-XML path doesn't raise `CorruptMetadataError`.** Inconsistent with a strict reading of ADR 0006; consistent with the EXIF + IPTC implementations. The class is defined but unused project-wide — separate cleanup either way.
- 🔄 **Reconsider compressed-iTXt support** if a real-world workflow surfaces a PNG whose XMP is only available compressed. Adding `zlib` decode is a few lines; the cost is mostly in test fixtures and the new error-path surface.
- 🔄 **Reconsider extended XMP** if a real consumer needs thumbnails-or-history from the second packet. The segment-walker can be extended to locate-and-merge.
- 🔄 **Reconsider TIFF host-format support** when TIFF support overall is added. Tag 700 is the standard location.
- 🔄 **Reconsider the friendly-prefix map** if a real workflow wants Lightroom-develop or another vendor namespace surfaced. Adding a prefix is one map entry; no logic change.
- 🔄 **Reconsider raising `CorruptMetadataError`** as part of a project-wide cleanup of the (currently unused) class — coordinate with the EXIF and IPTC implementations.
