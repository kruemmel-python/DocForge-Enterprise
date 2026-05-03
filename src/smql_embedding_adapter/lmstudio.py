"""LM Studio OpenAI-compatible client."""

from __future__ import annotations

import json
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .exceptions import EmbeddingError


@dataclass(slots=True)
class LMStudioClient:
    base_url: str = "http://127.0.0.1:1234/v1"
    embedding_model: str = "text-embedding-nomic-embed-text-v1.5"
    chat_model: str = "local-model"
    timeout_seconds: float = 120.0

    def _normalized_base_url(self) -> str:
        """Return a base URL suitable for LM Studio's OpenAI-compatible endpoints.

        LM Studio exposes OpenAI-compatible embeddings under /v1/embeddings.
        Operators often pass only http://127.0.0.1:1234; in that case we append
        /v1 instead of accidentally calling the unsupported /embeddings path.

        Non-root API bases such as http://127.0.0.1:1234/api/v0 are preserved.
        """

        raw = self.base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(raw)
        path = parsed.path.rstrip("/")
        if path in {"", "/"}:
            return urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, "/v1", parsed.query, parsed.fragment)
            ).rstrip("/")
        return raw

    def _url(self, path: str) -> str:
        return self._normalized_base_url().rstrip("/") + "/" + path.lstrip("/")

    def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._url(path),
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        attempts = 4
        last_error: BaseException | None = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body)
            except json.JSONDecodeError as exc:
                raise EmbeddingError("LM Studio returned invalid JSON") from exc
            except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
                text = str(getattr(exc, "reason", exc)).lower()
                timed_out = isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in text or "timeout" in text
                if not timed_out:
                    raise EmbeddingError(f"LM Studio request failed: {exc}") from exc
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep((2 ** attempt) * 1.5 + random.uniform(0.0, 0.3))
        raise EmbeddingError(f"LM Studio request timed out after {attempts} attempts: {last_error}")

    def embed(self, texts: Iterable[str]) -> list[list[float]]:
        inputs = list(texts)
        if not inputs:
            return []
        response = self._post(
            "/embeddings",
            {"model": self.embedding_model, "input": inputs},
        )
        data = response.get("data")
        if not isinstance(data, list):
            endpoint = self._url("/embeddings")
            raise EmbeddingError(
                "LM Studio embeddings response missing data "
                f"from {endpoint}: {response!r}"
            )

        by_index: dict[int, list[float]] = {}
        for item in data:
            if not isinstance(item, Mapping):
                continue
            index = int(item.get("index", len(by_index)))
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                raise EmbeddingError(f"Embedding item has no vector: {item!r}")
            by_index[index] = [float(v) for v in embedding]
        try:
            return [by_index[i] for i in range(len(inputs))]
        except KeyError as exc:
            raise EmbeddingError("LM Studio returned incomplete embedding batch") from exc

    def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.1) -> str:
        response = self._post(
            "/chat/completions",
            {
                "model": self.chat_model,
                "messages": messages,
                "temperature": temperature,
            },
        )
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise EmbeddingError(f"LM Studio chat response missing choices: {response!r}")
        msg = choices[0].get("message", {}) if isinstance(choices[0], Mapping) else {}
        content = msg.get("content") if isinstance(msg, Mapping) else None
        if not isinstance(content, str):
            raise EmbeddingError("LM Studio chat response missing message content")
        return content
