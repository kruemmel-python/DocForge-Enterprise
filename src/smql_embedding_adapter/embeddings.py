"""Embedding providers."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable
from typing import Protocol


class EmbeddingProvider(Protocol):
    def embed(self, texts: Iterable[str]) -> list[list[float]]:
        """Return one embedding per text."""


class DeterministicLocalEmbedder:
    """Dependency-free hash embedder for tests and offline smoke runs.

    It is not semantically equivalent to a real model. It creates stable vectors
    with token-level hashing so the full adapter can be tested without LM Studio.
    """

    def __init__(self, dimension: int = 384) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self.dimension = dimension

    def embed(self, texts: Iterable[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        tokens = re.findall(r"[\wäöüÄÖÜß]+", text.lower(), flags=re.UNICODE)
        if not tokens:
            tokens = [""]
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            idx = int.from_bytes(digest[:4], "little") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            weight = 0.5 + digest[5] / 255.0
            vec[idx] += sign * weight
        norm = math.sqrt(sum(v * v for v in vec))
        if norm:
            vec = [v / norm for v in vec]
        return vec
