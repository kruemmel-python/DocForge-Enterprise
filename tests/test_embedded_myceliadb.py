
from __future__ import annotations

import base64
import json
import socket
import struct
import threading
import urllib.request
from pathlib import Path

from myceliadb_embedded.gateway import start_server


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


def _post(url: str, command: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps({"command": command, "payload": payload}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _b64(values: list[float]) -> str:
    return base64.b64encode(struct.pack("<" + "f" * len(values), *values)).decode("ascii")


def test_embedded_gateway_store_and_find(tmp_path: Path) -> None:
    port = _free_port()
    httpd = start_server(port=port, root=tmp_path, quiet=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/"
    try:
        assert _post(url, "check_integrity", {})["status"] == "ok"
        stored = _post(
            url,
            "store_embedding",
            {
                "collection": "test",
                "id": "a",
                "dimension": 3,
                "vector_f32_b64": _b64([1.0, 0.0, 0.0]),
                "metadata": {"file_path": "a.py"},
            },
        )
        assert stored["status"] == "ok"

        found = _post(
            url,
            "find_embedding",
            {
                "collection": "test",
                "limit": 1,
                "dimension": 3,
                "query_vector_f32_b64": _b64([1.0, 0.0, 0.0]),
            },
        )
        assert found["status"] == "ok"
        assert found["full_dimension_search"] is True
        assert found["results"][0]["id"] == "a"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
