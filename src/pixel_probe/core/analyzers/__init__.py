"""Pixel-derived analyses (histogram, dominant colours, perceptual hashing, ...).

This package is a deliberate placeholder for v0.1. v0.1 ships only metadata
extractors (see sibling :mod:`pixel_probe.core.extractors`); pixel-derived
analyses live here because they have a different perf profile (CPU-bound,
always produce output) and a different conceptual question ("what does the
image *look* like?" vs the extractors' "what's *in* the file?").
"""
