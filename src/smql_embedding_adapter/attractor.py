"""High-dimensional embedding to compact attractor projections."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class AttractorProjection:
    mood_vector: tuple[float, float, float]
    energy_hash: str
    stability: float
    pheromone: float


class AttractorMapper:
    """Project embeddings into MyceliaDB-compatible compact fields.

    Current MyceliaDB SMQL ranks records through 3-dimensional mood vectors.
    This mapper keeps the full vector in the sidecar and projects a lossy
    3-bucket signature for compatibility with current SMQL.
    """

    @staticmethod
    def project(vector: Sequence[float], *, pheromone: float = 1.0) -> AttractorProjection:
        if not vector:
            raise ValueError("embedding vector is empty")
        thirds = (vector[0::3], vector[1::3], vector[2::3])
        buckets: list[float] = []
        for bucket in thirds:
            if not bucket:
                buckets.append(0.0)
                continue
            avg = sum(float(v) for v in bucket) / len(bucket)
            buckets.append(max(0.0, min(1.0, (avg + 1.0) / 2.0 if avg < 0 else avg)))
        norm = math.sqrt(sum(float(v) * float(v) for v in vector))
        stability = max(0.0, min(1.0, norm / (norm + 1.0)))
        raw = ",".join(f"{float(v):.7g}" for v in vector[:256]).encode("utf-8")
        energy_hash = hashlib.sha256(raw).hexdigest()
        return AttractorProjection(
            mood_vector=(buckets[0], buckets[1], buckets[2]),
            energy_hash=energy_hash,
            stability=stability,
            pheromone=max(0.0, min(1.0, float(pheromone))),
        )
