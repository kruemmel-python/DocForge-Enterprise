"""Small vector math primitives.

The default path avoids mandatory NumPy so the adapter remains offline and
dependency-free. If NumPy is installed, callers may add an accelerated path later
without changing the store format.
"""

from __future__ import annotations

import hashlib
import math
import struct
import sys
from array import array
from collections.abc import Iterable, Sequence


def coerce_float32_vector(values: Iterable[float]) -> array:
    vec = array("f", (float(v) for v in values))
    if sys.byteorder != "little":
        vec.byteswap()
    return vec


def vector_to_le_bytes(values: Sequence[float] | array) -> bytes:
    vec = values if isinstance(values, array) else coerce_float32_vector(values)
    if vec.typecode != "f":
        vec = array("f", (float(v) for v in vec))
    out = array("f", vec)
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def vector_from_le_bytes(raw: bytes) -> array:
    if len(raw) % 4 != 0:
        raise ValueError("float32 vector bytes must be divisible by 4")
    vec = array("f")
    vec.frombytes(raw)
    if sys.byteorder != "little":
        vec.byteswap()
    return vec


def l2_norm(values: Sequence[float]) -> float:
    return math.sqrt(sum(float(v) * float(v) for v in values))


def dot_memoryview(query: Sequence[float], row: memoryview) -> float:
    total = 0.0
    for a, b in zip(query, row, strict=True):
        total += float(a) * float(b)
    return total


def cosine_similarity(query: Sequence[float], row: Sequence[float], row_norm: float | None = None) -> float:
    qn = l2_norm(query)
    rn = row_norm if row_norm is not None else l2_norm(row)
    if qn == 0.0 or rn == 0.0:
        return 0.0
    return max(-1.0, min(1.0, sum(float(a) * float(b) for a, b in zip(query, row, strict=True)) / (qn * rn)))


def cosine_similarity_memoryview(query: Sequence[float], query_norm: float, row: memoryview, row_norm: float) -> float:
    if query_norm == 0.0 or row_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot_memoryview(query, row) / (query_norm * row_norm)))


def sha256_vector(values: Sequence[float] | array) -> str:
    return hashlib.sha256(vector_to_le_bytes(values)).hexdigest()


def pack_u32(value: int) -> bytes:
    return struct.pack("<I", value)


def unpack_u32(raw: bytes) -> int:
    return struct.unpack("<I", raw)[0]
