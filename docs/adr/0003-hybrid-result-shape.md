# ADR 0003 — Hybrid result shape: dataclass envelope + typed payload

## Status

Accepted (2026-04-26).

## Context

Each extractor needs to return its parsed metadata. Three reasonable shapes were on the table:

- **Pure dicts** — `Mapping[str, Any]` for everything, including warnings/errors as keys. Trivial to implement and serialize. No mypy help; readers can't tell what fields exist without running the code.
- **Pure dataclasses** — one bespoke dataclass per metadata category, every field enumerated. Strong typing. But EXIF alone has hundreds of possible tags, almost all optional and most rarely populated; enumerating them is impractical and lies about how dynamic the schema actually is.
- **Hybrid** — dataclass envelope around a typed payload that captures dynamism honestly.

EXIF, IPTC, and XMP have genuinely dynamic schemas. The envelope (`extractor_name`, `data`, `warnings`, `errors`) is structurally fixed; the *payload* schema isn't.

## Decision

`ExtractorResult` is a frozen, generic dataclass:

```python
@dataclass(frozen=True)
class ExtractorResult(Generic[T]):
    extractor_name: str
    data: T | None = None
    warnings: tuple[str, ...] = ()
    errors:   tuple[str, ...] = ()
```

`T` is the per-extractor payload type:

- `FileInfoExtractor(Extractor[FileInfo])` — concrete dataclass for the small, stable schema
- `ExifExtractor(Extractor[ExifData])` where `ExifData = dict[str, Any]` — module-level type alias keeps signatures from drowning in `dict[str, Any]` repetitions and lets us tighten the alias later (e.g. to a `TypedDict`) without rippling to every callsite
- `IptcExtractor(Extractor[IptcData])` where `IptcData = dict[str, str | list[str]]`
- `XmpExtractor(Extractor[XmpData])` where `XmpData = dict[str, dict[str, Any]]`

`data: T | None` because the orchestrator constructs error-only results (`data=None`) when an extractor raises. Successful extractions always populate `data`.

## Consequences

- ✅ Structure where structure helps (the envelope), flexibility where the data is genuinely dynamic (the payload).
- ✅ Type aliases keep signatures readable: `Extractor[ExifData]` reads better than `Extractor[dict[str, Any]]` and lets us tighten the alias later without rippling.
- ✅ mypy narrows `result.data` correctly per extractor — `FileInfoExtractor.extract()` returns `ExtractorResult[FileInfo]`, not the abstract base.
- ✅ `frozen=True` + `tuple[str, ...]` for warnings/errors prevent accidental mutation — both the reference *and* the contents are immutable.
- ❌ For most extractors `T = dict[str, Any]`, so the generic adds modest value beyond `FileInfo`. Defensible but borderline.
- 🔄 Reconsider when a payload's schema becomes stable enough to deserve its own dataclass (e.g., if we ever decide to enumerate the EXIF tags we actually surface).
