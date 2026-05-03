"""Client for the local MyceliaDB Zero-Logic-Gateway."""

from __future__ import annotations

import base64
import json
import random
import socket
import struct
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .attractor import AttractorMapper
from .exceptions import MyceliaGatewayError
from .smql import SMQLQuery
from .types import EmbeddingRecord


@dataclass(slots=True)
class MyceliaGatewayClient:
    base_url: str = "http://127.0.0.1:9999"
    token: str = ""
    timeout_seconds: float = 30.0
    smql_table: str = "mycelia_embeddings"


    @staticmethod
    def _vector_to_f32_b64(vector: Sequence[float]) -> str:
        """Encode a vector as little-endian float32 base64 for MyceliaDB v1.22b.

        JSON arrays remain human-readable but force the gateway to parse thousands
        of Python floats.  The v1.22b path uses a compact binary slab payload;
        MyceliaDB verifies the SHA-256 against the adapter's vector hash.
        """
        buf = bytearray()
        pack = struct.Struct("<f").pack
        for value in vector:
            buf += pack(float(value))
        return base64.b64encode(bytes(buf)).decode("ascii")

    def call(self, command: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request_body = json.dumps(
            {"command": command, "payload": dict(payload or {})},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Mycelia-Local-Token"] = self.token
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/",
            data=request_body,
            headers=headers,
            method="POST",
        )
        attempts = 4
        last_error: BaseException | None = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                raise MyceliaGatewayError(f"MyceliaDB HTTP {exc.code}: {body}") from exc
            except json.JSONDecodeError as exc:
                raise MyceliaGatewayError("MyceliaDB returned invalid JSON") from exc
            except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
                text = str(getattr(exc, "reason", exc)).lower()
                timed_out = isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in text or "timeout" in text
                if not timed_out:
                    raise MyceliaGatewayError(f"MyceliaDB unavailable: {exc}") from exc
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep((2 ** attempt) * 1.0 + random.uniform(0.0, 0.3))
        raise MyceliaGatewayError(f"MyceliaDB timed out after {attempts} attempts: {last_error}")

    def probe_connection(self) -> dict[str, Any]:
        """Session-free MyceliaDB reachability probe.

        MyceliaDB v1.21 protects many read/status commands behind the rotating
        Engine-Session layer.  ``check_integrity`` is intentionally allowed before
        login and therefore is the safest way for an external sidecar to prove
        that the local transport token is accepted without consuming or requiring
        an Engine request token.
        """
        return self.call("check_integrity", {})

    def transport_status(self) -> dict[str, Any]:
        """Protected MyceliaDB transport-status command.

        This command may return "Engine-Session fehlt" on current MyceliaDB
        builds.  Use :meth:`probe_connection` for unauthenticated diagnostics.
        """
        return self.call("local_transport_security_status", {})


    def find_embedding(
        self,
        vector: Sequence[float],
        *,
        collection: str,
        limit: int = 10,
        strict_vram_required: bool = False,
    ) -> dict[str, Any]:
        """v1.22b full-dimensional retrieval through MyceliaDB.

        The request includes the query vector as little-endian float32 base64.
        v1.22b MyceliaDB builds can rank stored 768D/1024D vectors inside the
        MyceliaDB process and return top-k records.  Older v1.22a builds ignore
        the vector and respond as projection-only; the adapter detects that and
        falls back to the local vault unless ``search_backend=mycelia`` or strict
        VRAM mode is requested.
        """
        projection = AttractorMapper.project(vector)
        return self.call(
            "find_embedding",
            {
                "collection": collection,
                "limit": int(limit),
                "dimension": len(vector),
                "query_vector_f32_b64": self._vector_to_f32_b64(vector),
                "vector_encoding": "float32-le-base64",
                "mood_vector": list(projection.mood_vector),
                "energy_hash": projection.energy_hash,
                "strict_vram_required": strict_vram_required,
            },
        )


    def vector_index_status(self) -> dict[str, Any]:
        """v1.22b native vector-index diagnostics."""
        return self.call("smql_vector_index_status", {})


    def sealed_abi_status(self) -> dict[str, Any]:
        """v1.22c sealed native ABI diagnostics."""
        return self.call("smql_sealed_abi_status", {})

    def forensic_attestation(self, *, collection: str | None = None) -> dict[str, Any]:
        """Return the current v1.22c forensic residency attestation."""
        payload: dict[str, Any] = {}
        if collection:
            payload["collection"] = collection
        return self.call("smql_forensic_attestation", payload)

    def find_embedding_sealed(
        self,
        vector: Sequence[float],
        *,
        collection: str,
        limit: int = 10,
        strict_vram_required: bool = False,
        strict_no_cpu_ram_required: bool = False,
    ) -> dict[str, Any]:
        """v1.22c retrieval through the sealed ABI contract.

        For normal LM Studio text queries the query vector has already crossed the
        Python process.  MyceliaDB may still perform sealed native ranking, but it
        must return ``strict_vram_residency_proven=false`` unless the native ABI
        has a true sealed-handle input path.  When ``strict_no_cpu_ram_required``
        is true, MyceliaDB is expected to fail closed unless such a proof exists.
        """
        projection = AttractorMapper.project(vector)
        return self.call(
            "find_embedding_sealed",
            {
                "collection": collection,
                "limit": int(limit),
                "dimension": len(vector),
                "query_vector_f32_b64": self._vector_to_f32_b64(vector),
                "vector_encoding": "float32-le-base64",
                "mood_vector": list(projection.mood_vector),
                "energy_hash": projection.energy_hash,
                "strict_vram_required": strict_vram_required,
                "strict_no_cpu_ram_required": strict_no_cpu_ram_required,
                "transport_grade": "python-http-float32",
            },
        )

    def store_embedding_sealed(
        self,
        record: EmbeddingRecord,
        vector: Sequence[float],
        *,
        strict_vram_required: bool = False,
        strict_no_cpu_ram_required: bool = False,
    ) -> dict[str, Any]:
        """v1.22c store through the sealed ABI contract.

        This command asks MyceliaDB to use its sealed native ABI if available.  It
        sends float32 bytes for compatibility with LM Studio's HTTP embedding
        output; therefore strict no-CPU-RAM proof can only become true when
        MyceliaDB has a native sealed-handle ingress path and attests it.
        """
        projection = AttractorMapper.project(vector, pheromone=record.pheromone)
        payload = {
            "version": "MYCELIA_SMQL_EMBEDDING_V1_22C_SEALED_ABI",
            "collection": record.collection,
            "id": record.id,
            "dimension": record.dimension,
            "vector_sha256": record.vector_sha256,
            "payload_sha256": record.payload_sha256,
            "offset": record.offset,
            "norm": record.norm,
            "pheromone": record.pheromone,
            "mood_vector": list(projection.mood_vector),
            "energy_hash": projection.energy_hash,
            "stability": projection.stability,
            "metadata": record.metadata,
            "strict_vram_required": strict_vram_required,
            "strict_no_cpu_ram_required": strict_no_cpu_ram_required,
            "vector_encoding": "float32-le-base64",
            "vector_f32_b64": self._vector_to_f32_b64(vector),
            "transport_grade": "python-http-float32",
        }
        try:
            response = self.call("store_embedding_sealed", payload)
        except MyceliaGatewayError as exc:
            if strict_vram_required or strict_no_cpu_ram_required:
                raise
            return {"status": "unavailable", "message": str(exc), "mode": "v122b-or-sidecar"}

        if response.get("status") == "error" and "Unbekannter Befehl" in str(response.get("message", "")):
            if strict_vram_required or strict_no_cpu_ram_required:
                raise MyceliaGatewayError("MyceliaDB has no v1.22c store_embedding_sealed command")
            return {"status": "unavailable", "message": response.get("message"), "mode": "v122b-or-sidecar"}
        return response

    def explain(self, query: SMQLQuery) -> dict[str, Any]:
        return self.call("smql_explain", {"query": query.to_mycelia_compat()})

    def smql_query(self, query: SMQLQuery, *, debug: bool = False) -> dict[str, Any]:
        return self.call("smql_query", {"query": query.to_mycelia_compat(), "debug": debug})

    def store_embedding(
        self,
        record: EmbeddingRecord,
        vector: Sequence[float],
        *,
        strict_vram_required: bool = False,
    ) -> dict[str, Any]:
        """Future-native v1.22 operation.

        Current MyceliaDB packages may return "unknown command". The adapter treats
        that as unavailable and keeps the sidecar result authoritative.
        """
        projection = AttractorMapper.project(vector, pheromone=record.pheromone)
        payload = {
            "version": "MYCELIA_SMQL_EMBEDDING_V1_22_DRAFT",
            "collection": record.collection,
            "id": record.id,
            "dimension": record.dimension,
            "vector_sha256": record.vector_sha256,
            "payload_sha256": record.payload_sha256,
            "offset": record.offset,
            "norm": record.norm,
            "pheromone": record.pheromone,
            "mood_vector": list(projection.mood_vector),
            "energy_hash": projection.energy_hash,
            "stability": projection.stability,
            "metadata": record.metadata,
            "strict_vram_required": strict_vram_required,
            "vector_encoding": "float32-le-base64",
            "vector_f32_b64": self._vector_to_f32_b64(vector),
        }
        try:
            response = self.call("store_embedding", payload)
        except MyceliaGatewayError as exc:
            if strict_vram_required:
                raise
            return {"status": "unavailable", "message": str(exc), "mode": "sidecar-only"}

        if response.get("status") == "error" and "Unbekannter Befehl" in str(response.get("message", "")):
            if strict_vram_required:
                raise MyceliaGatewayError("MyceliaDB has no native store_embedding command")
            return {"status": "unavailable", "message": response.get("message"), "mode": "sidecar-only"}
        return response
