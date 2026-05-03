from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json
import urllib.request
import urllib.error
import urllib.parse


@dataclass(slots=True)
class HttpResult:
    ok: bool
    status: int
    url: str
    content_type: str
    text: str
    json_data: Any | None
    error: str = ""

    @property
    def is_json(self) -> bool:
        return self.json_data is not None


def request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 8.0,
) -> HttpResult:
    body = None
    h = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    h.setdefault("Accept", "application/json")
    req = urllib.request.Request(url=url, data=body, headers=h, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            ct = resp.headers.get("Content-Type", "")
            jd = None
            try:
                jd = json.loads(text)
            except Exception:
                jd = None
            return HttpResult(True, int(resp.status), url, ct, text, jd)
    except urllib.error.HTTPError as e:
        raw = e.read()
        text = raw.decode("utf-8", errors="replace")
        jd = None
        try:
            jd = json.loads(text)
        except Exception:
            jd = None
        return HttpResult(False, int(e.code), url, e.headers.get("Content-Type", ""), text, jd, f"HTTP {e.code}")
    except Exception as e:
        return HttpResult(False, 0, url, "", "", None, repr(e))


def join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")
