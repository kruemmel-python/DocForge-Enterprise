"""Dependency-free HTTP sidecar server."""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from .adapter import EmbeddingAdapter
from .config import Settings

LOGGER = logging.getLogger("smql_embedding_adapter.server")


class AdapterHTTPHandler(BaseHTTPRequestHandler):
    adapter: EmbeddingAdapter

    def _send_json(self, status: int, body: Mapping[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _request_charset(self) -> str:
        content_type = self.headers.get("Content-Type", "")
        for part in content_type.split(";"):
            item = part.strip()
            if item.lower().startswith("charset="):
                charset = item.split("=", 1)[1].strip().strip("\"'")
                if charset:
                    return charset
        return "utf-8"

    @staticmethod
    def _decode_json_body(raw: bytes, preferred_encoding: str = "utf-8") -> str:
        """Decode local JSON requests robustly.

        JSON clients should send UTF-8, but Windows PowerShell 5.x can submit
        ``-Body`` strings with the active ANSI code page when no explicit byte
        array is used.  A German prompt such as ``vollständigen`` then reaches
        the adapter as byte ``0xe4`` and strict UTF-8 decoding fails before the
        RAG request can run.  For this local adapter we accept UTF-8/UTF-8-BOM
        first and fall back to common Windows encodings instead of returning a
        non-JSON error to the PHP bridge.
        """
        encodings = [preferred_encoding, "utf-8-sig", "utf-8", "cp1252", "latin-1"]
        seen: set[str] = set()
        for encoding in encodings:
            enc = (encoding or "utf-8").lower()
            if enc in seen:
                continue
            seen.add(enc)
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
            except LookupError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw_bytes = self.rfile.read(length)
        raw = self._decode_json_body(raw_bytes, self._request_charset())
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            store = self.adapter.store()
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "SMQL-Embedding-Adapter",
                    "collection": store.collection,
                    "records": store.count,
                    "dimension": store.dimension,
                    "merkle_head": store.ledger.head,
                },
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            data = self._read_json()
            match self.path.rstrip("/"):
                case "/v1/ingest":
                    texts = data.get("texts", [])
                    if not isinstance(texts, list) or not all(isinstance(x, str) for x in texts):
                        raise ValueError("texts must be a list of strings")
                    ids = data.get("ids")
                    metadata = data.get("metadata")
                    result = self.adapter.ingest_texts(
                        texts,
                        ids=ids if isinstance(ids, list) else None,
                        metadata=metadata if isinstance(metadata, list) else None,
                        collection=str(data.get("collection") or self.adapter.settings.adapter.default_collection),
                        store_text=bool(data.get("store_text", self.adapter.settings.adapter.store_text_default)),
                    )
                    self._send_json(HTTPStatus.OK, self.adapter.result_to_json(result))
                case "/v1/ingest_embeddings":
                    vectors = data.get("embeddings", [])
                    ids = data.get("ids", [])
                    if not isinstance(vectors, list) or not isinstance(ids, list):
                        raise ValueError("embeddings and ids must be lists")
                    result = self.adapter.ingest_embeddings(
                        vectors,
                        ids=[str(x) for x in ids],
                        metadata=data.get("metadata") if isinstance(data.get("metadata"), list) else None,
                        texts=data.get("texts") if isinstance(data.get("texts"), list) else None,
                        collection=str(data.get("collection") or self.adapter.settings.adapter.default_collection),
                        store_text=bool(data.get("store_text", self.adapter.settings.adapter.store_text_default)),
                    )
                    self._send_json(HTTPStatus.OK, self.adapter.result_to_json(result))
                case "/v1/query":
                    limit = int(data.get("limit", 10))
                    collection = str(data.get("collection") or self.adapter.settings.adapter.default_collection)
                    if isinstance(data.get("embedding"), list):
                        result = self.adapter.query_embedding(data["embedding"], collection=collection, limit=limit)
                    else:
                        text = str(data.get("text", ""))
                        result = self.adapter.query_text(text, collection=collection, limit=limit)
                    self._send_json(HTTPStatus.OK, self.adapter.result_to_json(result))
                case "/v1/smql":
                    query = str(data.get("query", ""))
                    collection = str(data.get("collection") or self.adapter.settings.adapter.default_collection)
                    result = self.adapter.query_smql(query, collection=collection)
                    self._send_json(HTTPStatus.OK, self.adapter.result_to_json(result))
                case "/v1/rag_chat":
                    question = str(data.get("question", "") or data.get("text", ""))
                    collection = str(data.get("collection") or self.adapter.settings.adapter.default_collection)
                    result = self.adapter.rag_chat(
                        question,
                        collection=collection,
                        limit=int(data.get("limit", 4)),
                        temperature=float(data.get("temperature", 0.15)),
                        system_prompt=data.get("system_prompt") if isinstance(data.get("system_prompt"), str) else None,
                        max_context_chars=int(data.get("max_context_chars", 12000)),
                    )
                    status = HTTPStatus.OK if result.get("status") == "ok" else HTTPStatus.BAD_REQUEST
                    self._send_json(status, result)
                case "/v1/collections/reset":
                    collection = str(data.get("collection") or self.adapter.settings.adapter.default_collection)
                    store = self.adapter.store(collection)
                    store.reset()
                    self._send_json(
                        HTTPStatus.OK,
                        {"status": "ok", "collection": collection, "merkle_head": store.ledger.head},
                    )
                case _:
                    self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "not found"})
        except Exception as exc:
            LOGGER.exception("request failed")
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)


def make_handler(adapter: EmbeddingAdapter) -> type[AdapterHTTPHandler]:
    class BoundHandler(AdapterHTTPHandler):
        pass

    BoundHandler.adapter = adapter
    return BoundHandler


def run_server(settings: Settings, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    adapter = EmbeddingAdapter(settings)
    server = ThreadingHTTPServer((host, port), make_handler(adapter))
    LOGGER.info("SMQL Embedding Adapter listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("shutdown requested")
    finally:
        server.server_close()
