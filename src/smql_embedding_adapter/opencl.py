"""OpenCL integration placeholder.

This module intentionally does not import pyopencl by default. The repository
ships an OpenCL kernel file under ``kernels/cosine_similarity.cl`` so native
MyceliaDB or a future optional pyopencl path can compile the same algorithm.
"""

from __future__ import annotations

from pathlib import Path


def kernel_source(path: str | Path | None = None) -> str:
    if path is None:
        path = Path(__file__).resolve().parents[2] / "kernels" / "cosine_similarity.cl"
    return Path(path).read_text(encoding="utf-8")
