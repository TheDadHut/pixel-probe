# ADR 0008 — EXIF parsing strategy

## Status

Accepted (2026-04-29).

## Context

EXIF metadata is structured as IFD (Image File Directory) blocks: a main IFD plus several sub-IFDs reachable by tag pointer. The EXIF spec defines at least three sub-IFD pointers in the main IFD:

- **`ExifIFDPointer`** (`0x8769`) — tags users actually care about: `ExposureTime`, `FNumber`, `ISOSpeedRatings`, `FocalLength`, `DateTimeOriginal`, etc.
- **`GPSInfo`** (`0x8825`) — GPS coordinates as DMS rationals plus refs (N/S/E/W).
- **`InteroperabilityIFDPointer`** (`0xA005`) — image interoperability metadata (related-image-file format, thumbnail interop). Rarely useful in practice.

There's also the question of how to handle bytes-valued tags whose payloads are vendor-specific blobs — most importantly `MakerNote` (`0x927C`), which carries proprietary camera-vendor data and routinely runs into the kilobytes.

The architectural questions:

1. **How do we surface sub-IFD tags?** Three reasonable shapes:
   a. Flatten everything into one big dict (no sub-dicts).
   b. Mirror the file structure: every sub-IFD becomes its own sub-dict in `data` (`data["exif"]`, `data["gps"]`, `data["interop"]`).
   c. Hybrid: flatten where the sub-IFD is just a continuation of the main one's logical contents; sub-dict where the sub-IFD is a coordinated unit consumers want to access together.
2. **Do we walk Interop?** It's the same code pattern as Exif and GPS — easy to add. But its tags are rarely consumed.
3. **What do we do with vendor blobs?** Carry the raw bytes through the result envelope, or summarize?

## Decision

### Sub-IFD walking — hybrid output shape

- **Main IFD tags** flatten into the result's top-level `data` dict.
- **Exif sub-IFD tags** *also* flatten into `data` (alongside main-IFD tags). Tag IDs are unique across IFDs so there's no collision; consumers don't care which IFD a tag came from semantically (`ExposureTime` is `ExposureTime`).
- **GPS sub-IFD tags** become a `data["gps"]` sub-dict, **not** flattened. GPS tags are only useful as a coordinated set (lat + lat-ref + lon + lon-ref + altitude + altitude-ref + ...); flattening would scatter them across the top-level dict and lose that grouping.
- **Sub-IFD pointer tags** (`ExifIFDPointer`, `GPSInfo`, `InteroperabilityIFDPointer`) are filtered out of the main-IFD walk via the `_SUB_IFD_POINTERS` frozenset — we walk the sub-IFD explicitly, so the raw pointer integer would be useless noise in the result.
- **Interop sub-IFD is not walked.** Its pointer tag is in `_SUB_IFD_POINTERS` so the pointer integer is suppressed, but the sub-IFD's contents are deliberately dropped on the floor.

### GPS convenience fields

Inside `data["gps"]`, alongside the raw DMS rational tuples (`GPSLatitude = (37, 46, 30)` etc.), surface convenience decimal-degree fields: `gps["latitude"]` and `gps["longitude"]` as signed `float`s. The sign is taken from the corresponding `*Ref` tag (S/W → negative). Both raw and decimal forms ship — different consumers want different things.

### Bytes-summarization gate

In `_normalize`, any `bytes` value whose length exceeds `_MAX_BYTES_INLINE` (currently 64) is replaced by a summary string of the form `"<binary, N bytes>"`. The cap is a `Final` constant; tests can `monkeypatch` it lower to exercise the gate without crafting oversized fixtures.

This gate applies uniformly to every tag in every IFD — not only `MakerNote`. Any vendor / future tag with a multi-KB blob is summarized.

## Consequences

- ✅ **Consumer-friendly flat surface for the common tags.** A user iterating `result.data` to render "camera info" sees `ExposureTime` next to `Make` next to `Model`, regardless of which IFD each lived in. Mirrors how every EXIF tool surfaces these tags.
- ✅ **GPS stays cohesive.** UI consumers can do `result.data.get("gps")` once and get the whole geolocation block; they don't need to know the spec's tag list.
- ✅ **No useless pointer integers in the output.** Filtering the pointer tags upfront keeps the top-level dict clean.
- ✅ **Vendor blobs can't carry adversarial bytes through the result envelope.** A 5 MB `MakerNote` becomes `"<binary, 5242880 bytes>"` — informative, not dangerous, JSON-serializable.
- ✅ **Each sub-IFD is independently fault-isolated.** Corrupt Exif sub-IFD → one error, GPS still parses. Corrupt GPS sub-IFD → one error, main IFD still ships.
- ❌ **Interop tags are dropped silently.** A user who needs them (rare) would have to parse the file directly. Acceptable for v0.1; a future extension would walk the IFD with a bare `data["interop"]` mirror of the GPS pattern.
- ❌ **A legitimate camera model name longer than 64 bytes would be summarized as binary.** Bound is generous for real-world EXIF strings but tight for niche vendor tags. The constant is tunable.
- ❌ **The flatten-Exif-but-sub-dict-GPS asymmetry is two rules to remember instead of one.** Documented inline + here; pays back via simpler downstream consumers.
- 🔄 **Reconsider walking Interop** if a consumer actually needs related-image-file format or thumbnail-interop tags. Mechanically trivial — just add a fourth walk.
- 🔄 **Reconsider the bytes cap** if a real workflow surfaces a tag value over 64 bytes that's legitimately needed. Raising the cap is one constant change.
- 🔄 **Reconsider the GPS-only sub-dict pattern** if a future sub-IFD (e.g. Interop, or some Phase-3 additions like XMP-attached EXIF blocks) has the same coordinated-unit shape. Pattern is the precedent.
