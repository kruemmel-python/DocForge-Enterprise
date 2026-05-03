
from __future__ import annotations

import base64
import json
import secrets
import struct
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from smql_embedding_adapter.store import MMapVectorStore
from smql_embedding_adapter.vector_math import sha256_vector


def _decode_f32_b64(value: str, dimension: int | None = None) -> list[float]:
    raw = base64.b64decode(value.encode("ascii"), validate=True)
    if len(raw) % 4 != 0:
        raise ValueError("float32 base64 payload length is not divisible by 4")
    count = len(raw) // 4
    if dimension is not None and int(dimension) != count:
        raise ValueError(f"dimension mismatch: payload has {count}, request says {dimension}")
    fmt = "<" + ("f" * count)
    return [float(x) for x in struct.unpack(fmt, raw)]


def _record_to_result(record: Any, *, score: float = 0.0, cosine: float = 0.0) -> dict[str, Any]:
    return {
        "id": record.id,
        "collection": record.collection,
        "score": float(score),
        "cosine": float(cosine),
        "pheromone": float(record.pheromone),
        "offset": int(record.offset),
        "dimension": int(record.dimension),
        "norm": float(record.norm),
        "vector_sha256": record.vector_sha256,
        "payload_sha256": record.payload_sha256,
        "created_at": float(record.created_at),
        "metadata": dict(record.metadata),
        "text_preview": record.text_preview,
        "text": record.text,
        "signature": record.mycelia_signature,
    }


class EmbeddedMyceliaEngine:
    """Small local MyceliaDB-compatible vector gateway.

    The engine intentionally implements the commands needed by DocForge and the
    SMQL Embedding Adapter instead of pretending to be the full historical
    MyceliaDB web platform. It stores vectors in the same append-only mmap layout
    used by the adapter and returns MyceliaDB-style metadata, residency flags and
    forensic status fields.
    """

    def __init__(self, root: Path, *, token: str = "", default_dimension: int | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.default_dimension = default_dimension
        self.started_at = time.time()
        self.gateway_id = secrets.token_hex(8)

    def store(self, collection: str, dimension: int | None = None) -> MMapVectorStore:
        return MMapVectorStore(self.root, collection=collection, dimension=dimension or self.default_dimension)

    def handle(self, command: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        match command:
            case "check_integrity":
                return self.check_integrity()
            case "local_transport_security_status":
                return self.transport_status()
            case "smql_vector_index_status":
                return self.vector_index_status(payload)
            case "smql_sealed_abi_status":
                return self.sealed_abi_status()
            case "smql_forensic_attestation":
                return self.forensic_attestation(payload)
            case "store_embedding" | "store_embedding_sealed":
                return self.store_embedding(payload, sealed=command.endswith("_sealed"))
            case "find_embedding" | "find_embedding_sealed":
                return self.find_embedding(payload, sealed=command.endswith("_sealed"))
            case "smql_explain":
                return self.smql_explain(payload)
            case "smql_query":
                return self.smql_query(payload)
            case _:
                return {"status": "error", "message": f"Unbekannter Befehl: {command}"}

    def check_integrity(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": "EMBEDDED_MYCELIADB_DOCFORGE_V1",
            "gateway_id": self.gateway_id,
            "mode": "embedded",
            "root": str(self.root),
            "uptime_seconds": round(time.time() - self.started_at, 3),
        }

    def transport_status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": "embedded-local-transport",
            "token_required": bool(self.token),
            "token_present": bool(self.token),
            "loopback_only": True,
        }

    def sealed_abi_status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": "EMBEDDED_MYCELIA_SEALED_ABI_COMPAT_V1",
            "sealed_abi_active": False,
            "strict_vram_residency_proven": False,
            "message": "Embedded gateway provides API compatibility, not native sealed VRAM proof.",
        }

    def forensic_attestation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        collection = str(payload.get("collection", "") or "")
        return {
            "status": "ok",
            "version": "EMBEDDED_MYCELIA_FORENSIC_ATTESTATION_V1",
            "collection": collection,
            "backend": "embedded-mmap",
            "sealed_abi_active": False,
            "vram_resident": False,
            "strict_vram_residency_proven": False,
            "proof_id": self.gateway_id,
            "proof_flags": ["local", "append-only-ledger", "mmap-vector-store"],
            "created_at": time.time(),
        }

    def vector_index_status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        collections: list[dict[str, Any]] = []
        if self.root.exists():
            for child in sorted(self.root.iterdir()):
                if not child.is_dir():
                    continue
                try:
                    store = MMapVectorStore(self.root, collection=child.name)
                    collections.append(
                        {
                            "collection": child.name,
                            "count": store.count,
                            "dimension": store.dimension,
                            "merkle_head": store.ledger.head,
                        }
                    )
                except Exception as exc:
                    collections.append({"collection": child.name, "status": "error", "message": str(exc)})
        return {
            "status": "ok",
            "version": "EMBEDDED_MYCELIA_VECTOR_INDEX_V1",
            "backend": "embedded-mmap",
            "collections": collections,
            "count": len(collections),
        }

    def store_embedding(self, payload: Mapping[str, Any], *, sealed: bool) -> dict[str, Any]:
        collection = str(payload.get("collection") or "default")
        record_id = str(payload.get("id") or "")
        if not record_id:
            return {"status": "error", "message": "id is required"}
        dimension = int(payload.get("dimension") or 0) or None
        encoded = str(payload.get("vector_f32_b64") or "")
        if not encoded:
            return {"status": "error", "message": "vector_f32_b64 is required"}
        vector = _decode_f32_b64(encoded, dimension=dimension)
        metadata = dict(payload.get("metadata", {}) if isinstance(payload.get("metadata", {}), Mapping) else {})
        text = payload.get("text")
        store = self.store(collection, dimension=len(vector))
        record = store.append(
            id=record_id,
            vector=vector,
            metadata=metadata,
            text=str(text) if text is not None else None,
            store_text=text is not None,
            pheromone=float(payload.get("pheromone", 1.0)),
            mycelia_signature=f"embedded:{self.gateway_id}:{sha256_vector(vector)[:16]}",
            replace_existing=True,
        )
        return {
            "status": "ok",
            "version": "EMBEDDED_MYCELIA_STORE_V1",
            "backend": "embedded-mmap",
            "sealed_abi_active": False,
            "vram_resident": False,
            "strict_vram_residency_proven": False,
            "collection": collection,
            "id": record.id,
            "dimension": record.dimension,
            "vector_sha256": record.vector_sha256,
            "payload_sha256": record.payload_sha256,
            "merkle_head": store.ledger.head,
            "mode": "sealed-compat" if sealed else "native-compat",
        }

    def find_embedding(self, payload: Mapping[str, Any], *, sealed: bool) -> dict[str, Any]:
        collection = str(payload.get("collection") or "default")
        limit = int(payload.get("limit") or 10)
        dimension = int(payload.get("dimension") or 0) or None
        encoded = str(payload.get("query_vector_f32_b64") or "")
        if not encoded:
            return {"status": "error", "message": "query_vector_f32_b64 is required"}
        query = _decode_f32_b64(encoded, dimension=dimension)
        store = self.store(collection, dimension=len(query))
        results = store.search(query, limit=limit)
        return {
            "status": "ok",
            "version": "EMBEDDED_MYCELIA_FIND_V1",
            "backend": "embedded-mmap",
            "collection": collection,
            "dimension": len(query),
            "count": len(results),
            "total_candidates": store.count,
            "full_dimension_search": True,
            "native_vector_search": True,
            "sealed_abi_active": False,
            "vram_resident": False,
            "strict_vram_residency_proven": False,
            "transport_grade": "python-http-float32",
            "proof_id": self.gateway_id,
            "proof_flags": ["embedded", "mmap", "ledger"],
            "merkle_head": store.ledger.head,
            "results": [
                _record_to_result(r.record, score=r.score, cosine=r.cosine)
                for r in results
            ],
            "mode": "sealed-compat" if sealed else "native-compat",
        }

    def smql_explain(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": "EMBEDDED_MYCELIA_SMQL_EXPLAIN_V1",
            "backend": "embedded-mmap",
            "query": payload.get("query"),
            "capabilities": ["find_embedding", "store_embedding", "vector_index_status"],
        }

    def smql_query(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": "error",
            "message": (
                "Embedded gateway accepts vector commands directly. Use the SMQL "
                "Embedding Adapter for parsing text SMQL into embeddings."
            ),
            "query": payload.get("query"),
        }


class EmbeddedMyceliaRequestHandler(BaseHTTPRequestHandler):
    engine: EmbeddedMyceliaEngine

    server_version = "EmbeddedMyceliaDB/0.1"

    def _json(self, status: int, body: Mapping[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/health", "/status"}:
            self._json(200, self.engine.check_integrity())
            return
        self._json(404, {"status": "error", "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.engine.token:
            supplied = self.headers.get("X-Mycelia-Local-Token", "")
            if supplied != self.engine.token:
                self._json(403, {"status": "error", "message": "local transport token mismatch"})
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 64 * 1024 * 1024:
                self._json(413, {"status": "error", "message": "request too large"})
                return
            body = self.rfile.read(length)
            request = json.loads(body.decode("utf-8"))
            command = str(request.get("command") or "")
            payload = request.get("payload") or {}
            if not isinstance(payload, Mapping):
                raise ValueError("payload must be an object")
            response = self.engine.handle(command, payload)
            status = 200 if response.get("status") != "error" else 400
            # Unknown command should remain HTTP 200 compatible with older clients.
            if "Unbekannter Befehl" in str(response.get("message", "")):
                status = 200
            self._json(status, response)
        except Exception as exc:  # defensive gateway boundary
            self._json(500, {"status": "error", "message": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(format, *args)



def start_server(
    *,
    host: str = "127.0.0.1",
    port: int = 9999,
    root: Path = Path(".docforge_workspace/embedded_myceliadb"),
    token: str = "",
    quiet: bool = False,
) -> ThreadingHTTPServer:
    """Create, but do not block on, an embedded MyceliaDB HTTP server."""
    engine = EmbeddedMyceliaEngine(root=root, token=token)
    handler = type(
        "DocForgeEmbeddedMyceliaRequestHandler",
        (EmbeddedMyceliaRequestHandler,),
        {"engine": engine},
    )
    httpd = ThreadingHTTPServer((host, int(port)), handler)
    setattr(httpd, "quiet", quiet)
    return httpd

def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 9999,
    root: Path = Path(".docforge_workspace/embedded_myceliadb"),
    token: str = "",
    quiet: bool = False,
) -> None:
    httpd = start_server(host=host, port=port, root=root, token=token, quiet=quiet)
    print(
        json.dumps(
            {
                "status": "ok",
                "service": "embedded-myceliadb",
                "url": f"http://{host}:{port}",
                "root": str(root),
                "token_required": bool(token),
            },
            ensure_ascii=False,
        )
    )
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
