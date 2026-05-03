"""Neural observer network that summarizes the biological world state."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from ..core.driver import OpenCLDriver
from ..core.gpu_tensor import GPUTensor


@dataclass
class Observation:
    """Compact representation of the biological layer."""

    embeddings: GPUTensor
    harmony_score: float
    tension_score: float


class ObserverNetwork:
    """Encoder that maps raw pheromone/nutrient tensors to embeddings."""

    def __init__(self, driver: OpenCLDriver, config: Dict[str, object]) -> None:
        self._driver = driver
        self._config = config
        self._embedding = GPUTensor(handle=0, shape=(256,), dtype="float32", payload=[])

    def _mean_payload(self, tensor: GPUTensor) -> float:
        payload = tensor.payload
        if payload is None:
            return 0.0
        if isinstance(payload, (int, float)):
            return float(payload)
        if isinstance(payload, list) and payload:
            return float(sum(payload) / len(payload))
        if isinstance(payload, tuple) and payload:
            return float(sum(payload) / len(payload))
        return 0.0

    def encode(self, snapshot_tensors: Dict[str, GPUTensor]) -> Observation:
        pheromones = snapshot_tensors["pheromone"]
        nutrients = snapshot_tensors["nutrient"]

        harmony = self._mean_payload(pheromones)
        nutrient_level = self._mean_payload(nutrients)
        if harmony == 0.0 and pheromones.payload is None:
            harmony = 0.5
        if nutrient_level == 0.0 and nutrients.payload is None:
            nutrient_level = 0.5
        tension = abs(harmony - nutrient_level)
        self._embedding.payload = [harmony, nutrient_level, tension]

        return Observation(embeddings=self._embedding, harmony_score=harmony, tension_score=tension)
