from __future__ import annotations

from pathlib import Path
from dataclasses import asdict, is_dataclass
import json
import urllib.parse
import time
import sys
import subprocess
import hashlib
from typing import Any

from .config import SuiteConfig
from .findings import Finding, Status, Severity
from .http_client import request_json, join_url
from .subproc import run_python_module
from .secret_scan import scan_paths
from .rehydration import audit_jsonl
from .rag_eval import load_cases, evaluate_rag_response


def timed(check_id: str, title: str, category: str):
    def deco(fn):
        def wrapper(cfg: SuiteConfig):
            start = time.time()
            try:
                f: Finding = fn(cfg)
            except Exception as e:
                f = Finding(
                    check_id=check_id,
                    title=title,
                    status=Status.FAIL,
                    severity=Severity.HIGH,
                    summary=f"Check crashed: {e!r}",
                    category=category,
                )
            f.check_id = check_id
            f.title = title
            f.category = category
            f.duration_ms = (time.time() - start) * 1000
            return f
        return wrapper
    return deco


@timed("CONN-ADAPTER-001", "SMQL Adapter health endpoint", "connectivity")
def check_adapter_health(cfg: SuiteConfig) -> Finding:
    r = request_json("GET", join_url(cfg.adapter_url, "/health"), timeout=cfg.http_timeout_seconds)
    if r.ok and r.is_json and r.json_data.get("status") == "ok":
        return Finding("", "", Status.PASS, Severity.INFO, "Adapter is reachable and healthy.", {"response": r.json_data})
    return Finding("", "", Status.FAIL, Severity.HIGH, "Adapter health endpoint failed.", {"status": r.status, "error": r.error, "preview": r.text[:500]})


@timed("CONN-LMSTUDIO-001", "LM Studio OpenAI-compatible models endpoint", "connectivity")
def check_lmstudio_models(cfg: SuiteConfig) -> Finding:
    r = request_json("GET", join_url(cfg.lmstudio_url, "/v1/models"), timeout=cfg.http_timeout_seconds)
    if r.ok and r.is_json:
        models = r.json_data.get("data", [])
        ids = [m.get("id") for m in models if isinstance(m, dict)]
        sev = Severity.INFO
        status = Status.PASS
        summary = "LM Studio /v1/models is reachable."
        if cfg.chat_model and cfg.chat_model not in ids:
            status = Status.WARN
            sev = Severity.MEDIUM
            summary = "LM Studio is reachable, but configured chat model was not listed."
        return Finding("", "", status, sev, summary, {"model_count": len(ids), "model_ids": ids[:30], "expected_chat_model": cfg.chat_model})
    return Finding("", "", Status.FAIL, Severity.HIGH, "LM Studio /v1/models failed.", {"status": r.status, "error": r.error, "preview": r.text[:500]})


@timed("GW-TOKEN-001", "MyceliaDB local transport token boundary", "gateway")
def check_mycelia_token_boundary(cfg: SuiteConfig) -> Finding:
    payload = {"command": "check_integrity"}
    no = request_json("POST", cfg.mycelia_url, payload=payload, timeout=cfg.http_timeout_seconds)
    bad = request_json("POST", cfg.mycelia_url, payload=payload, headers={"X-Mycelia-Local-Token": "definitely-wrong-token"}, timeout=cfg.http_timeout_seconds)
    token_present = Path(cfg.token_file).exists()
    good = None
    if token_present:
        token = Path(cfg.token_file).read_text(encoding="utf-8", errors="ignore").strip()
        good = request_json("POST", cfg.mycelia_url, payload=payload, headers={"X-Mycelia-Local-Token": token}, timeout=cfg.http_timeout_seconds)
    evidence = {
        "no_token": {"status": no.status, "json": no.json_data, "preview": no.text[:200]},
        "bad_token": {"status": bad.status, "json": bad.json_data, "preview": bad.text[:200]},
        "token_present": token_present,
        "good_token": {"status": good.status, "json": good.json_data} if good else None,
    }
    if not token_present:
        return Finding("", "", Status.FAIL, Severity.CRITICAL, "Local transport token file is missing.", evidence, "Restore local_transport.token or configure token_file.")
    good_ok = bool(good and good.is_json and str(good.json_data.get("status", "")).lower() in ("ok", "pass"))
    blocked = no.status in (401, 403) and bad.status in (401, 403)
    if good_ok and blocked:
        return Finding("", "", Status.PASS, Severity.INFO, "MyceliaDB token boundary behaves as expected.", evidence)
    if good_ok:
        return Finding("", "", Status.WARN, Severity.HIGH, "Good token works, but unauthenticated or bad-token boundary was not clearly rejected.", evidence)
    return Finding("", "", Status.FAIL, Severity.CRITICAL, "MyceliaDB did not accept the configured local transport token.", evidence)


@timed("MYC-STATUS-001", "MyceliaDB vector index and persistence status through adapter CLI", "myceliadb")
def check_mycelia_status(cfg: SuiteConfig) -> Finding:
    args = [
        "--mycelia-url", cfg.mycelia_url,
        "--mycelia-token-file", cfg.token_file,
        "mycelia-status",
    ]
    rr = run_python_module("smql_embedding_adapter.cli", args, cwd=cfg.adapter_root, timeout=60)
    data = rr.json_or_none()
    if rr.returncode != 0 or not isinstance(data, dict):
        return Finding("", "", Status.FAIL, Severity.HIGH, "Adapter CLI mycelia-status failed.", {"returncode": rr.returncode, "stdout": rr.stdout[:1000], "stderr": rr.stderr[:1000]})
    vi = data.get("vector_index", {})
    backend = vi.get("backend")
    total = int(vi.get("total_vectors") or 0)
    collections = vi.get("collections") or {}
    persistence = vi.get("persistence") or {}
    status = Status.PASS
    sev = Severity.INFO
    summary = "MyceliaDB status is healthy."
    if cfg.require_opencl_vram and backend != "opencl-vram":
        status, sev, summary = Status.FAIL, Severity.HIGH, "MyceliaDB vector index is not using opencl-vram."
    elif total <= 0:
        status, sev, summary = Status.WARN, Severity.HIGH, "MyceliaDB vector index has no vectors."
    elif cfg.collection not in collections:
        status, sev, summary = Status.WARN, Severity.MEDIUM, "Configured collection is not present in MyceliaDB vector index."
    return Finding("", "", status, sev, summary, {"mycelia_status": data, "backend": backend, "total_vectors": total, "collections": collections, "persistence": persistence})


@timed("WEB-API-001", "SCM Web Chat API is JSON-only", "web")
def check_web_chat_api_json(cfg: SuiteConfig) -> Finding:
    """Verify the Web Chat API, without turning a webroot miss into a hard enterprise failure.

    v2.0.4:
    - auto-detects common URLs
    - returns WARN for 404/webroot issues by default
    - returns FAIL only if web_chat_api_required=true or a configured deployment must expose the endpoint
    """
    def candidate_urls() -> list[str]:
        configured = (cfg.web_chat_api or "").strip()
        urls: list[str] = []
        def add(u: str) -> None:
            u = (u or "").strip()
            if u and u not in urls:
                urls.append(u)
        if configured and configured.lower() != "auto":
            add(configured)
            p = urllib.parse.urlparse(configured)
            if p.scheme and p.netloc:
                origin = f"{p.scheme}://{p.netloc}"
                for path in (
                    "/lmstudio_chat_api.php",
                    "/www/lmstudio_chat_api.php",
                    "/html/www/lmstudio_chat_api.php",
                    "/MyceliaDB/www/lmstudio_chat_api.php",
                ):
                    add(origin + path)
        else:
            for origin in ("http://127.0.0.1:8081", "http://localhost:8081", "http://127.0.0.1:8080", "http://localhost:8080", "http://127.0.0.1", "http://localhost"):
                for path in ("/lmstudio_chat_api.php", "/www/lmstudio_chat_api.php", "/html/www/lmstudio_chat_api.php"):
                    add(origin + path)
        return urls

    attempts = []
    html_non_404 = []
    statuses = []
    for url in candidate_urls():
        r = request_json("GET", url, timeout=cfg.http_timeout_seconds)
        statuses.append(r.status)
        item = {
            "url": url,
            "status": r.status,
            "content_type": r.content_type,
            "is_json": r.is_json,
            "preview": (r.text or "")[:300],
            "error": r.error,
        }
        attempts.append(item)
        if r.is_json:
            zero = r.json_data.get("zero_logic_safe", None) if isinstance(r.json_data, dict) else None
            configured = (cfg.web_chat_api or "").strip()
            if configured and configured.lower() != "auto" and url != configured:
                return Finding("", "", Status.WARN, Severity.MEDIUM,
                    "Configured Web Chat API URL returned no JSON, but an alternative JSON-only endpoint was found.",
                    {"configured_url": configured, "working_url": url, "status": r.status, "json": r.json_data,
                     "zero_logic_safe": zero, "attempts": attempts},
                    f"Set web_chat_api = \"{url}\" in configs/targets.example.toml.")
            return Finding("", "", Status.PASS if r.ok else Status.WARN, Severity.INFO,
                "Web Chat API returned JSON.",
                {"url": url, "status": r.status, "json": r.json_data, "zero_logic_safe": zero, "attempts": attempts})
        if r.status not in (0, 404):
            html_non_404.append(item)

    evidence = {
        "configured_url": cfg.web_chat_api,
        "attempts": attempts,
        "statuses": statuses,
        "tested_urls": [a["url"] for a in attempts],
        "html_non_404": html_non_404,
    }
    required = bool(getattr(cfg, "web_chat_api_required", False))
    all_404_or_unreachable = all(s in (0, 404) for s in statuses) if statuses else True

    if all_404_or_unreachable:
        return Finding("", "", Status.FAIL if required else Status.WARN, Severity.HIGH if required else Severity.MEDIUM,
            "Web Chat API was not found at the tested URLs. This is a webroot/configuration issue; adapter-level RAG is tested separately.",
            evidence,
            "Set web_chat_api to the working lmstudio_chat_api.php URL, or start PHP with the correct -t webroot.")

    return Finding("", "", Status.FAIL if required else Status.WARN, Severity.HIGH if required else Severity.MEDIUM,
        "No JSON-only Web Chat API endpoint was found. At least one tested URL returned HTML/text.",
        evidence,
        "Install the JSON-only lmstudio_chat_api.php hotfix or set web_chat_api to the correct URL.")

@timed("RAG-CHAT-001", "RAG chat baseline answer has sources and Mycelia backend", "rag")
def check_rag_chat_baseline(cfg: SuiteConfig) -> Finding:
    body = {"question": "Was ist MyceliaDB?", "collection": cfg.collection, "limit": cfg.rag_limit}
    r = request_json("POST", join_url(cfg.adapter_url, "/v1/rag_chat"), payload=body, timeout=45)
    if not (r.ok and r.is_json):
        return Finding("", "", Status.FAIL, Severity.HIGH, "Adapter /v1/rag_chat failed.", {"status": r.status, "error": r.error, "preview": r.text[:500]})
    data = r.json_data
    sources = data.get("sources") or []
    backend = str(data.get("retrieval_backend", ""))
    status = Status.PASS
    sev = Severity.INFO
    summary = "RAG chat baseline is healthy."
    if data.get("status") != "ok":
        status, sev, summary = Status.FAIL, Severity.HIGH, "RAG chat returned non-ok status."
    elif len(sources) < cfg.min_sources:
        status, sev, summary = Status.WARN, Severity.MEDIUM, "RAG chat returned too few sources."
    elif cfg.require_mycelia_backend and not backend.startswith("mycelia:"):
        status, sev, summary = Status.FAIL, Severity.HIGH, "RAG chat did not use MyceliaDB backend."
    elif cfg.require_opencl_vram and backend != "mycelia:opencl-vram":
        status, sev, summary = Status.WARN, Severity.MEDIUM, "RAG chat used MyceliaDB but not opencl-vram."
    return Finding("", "", status, sev, summary, {"retrieval_backend": backend, "source_count": len(sources), "context_chars": data.get("context_chars"), "answer_preview": str(data.get("answer", ""))[:600], "mycelia_native": data.get("mycelia_native")})


@timed("RAG-REDTEAM-001", "RAG prompt-injection and leak-resistance smoke corpus", "rag")
def check_rag_redteam(cfg: SuiteConfig) -> Finding:
    cases = load_cases(cfg.redteam_corpus)
    if not cases:
        return Finding("", "", Status.SKIP, Severity.INFO, "No RAG red-team corpus found.", {"path": cfg.redteam_corpus})
    results = []
    fail = warn = 0
    for case in cases:
        body = {"question": case.question, "collection": cfg.collection, "limit": cfg.rag_limit}
        r = request_json("POST", join_url(cfg.adapter_url, "/v1/rag_chat"), payload=body, timeout=60)
        if r.is_json:
            ev = evaluate_rag_response(case, r.json_data)
        else:
            from .rag_eval import RagEvaluation
            ev = RagEvaluation(case.id, "fail", "non-json response", {"status": r.status, "preview": r.text[:200]})
        results.append(ev.to_dict())
        if ev.status == "fail":
            fail += 1
        elif ev.status == "warn":
            warn += 1
    if fail:
        return Finding("", "", Status.FAIL, Severity.HIGH, f"{fail} RAG red-team cases failed.", {"case_count": len(cases), "fail": fail, "warn": warn, "results": results})
    if warn:
        return Finding("", "", Status.WARN, Severity.MEDIUM, f"{warn} RAG red-team cases produced warnings.", {"case_count": len(cases), "fail": fail, "warn": warn, "results": results})
    return Finding("", "", Status.PASS, Severity.INFO, "RAG red-team smoke corpus passed.", {"case_count": len(cases), "results": results})


@timed("DISK-SECRET-001", "Secret and token leakage scan in project files", "secrets")
def check_secret_scan(cfg: SuiteConfig) -> Finding:
    roots = [cfg.adapter_root, cfg.mycelia_root]
    token = None
    tf = Path(cfg.token_file)
    if tf.exists():
        token = tf.read_text(encoding="utf-8", errors="ignore").strip()
    hits = scan_paths(roots, cfg.secret_scan_extensions, cfg.scan_max_file_mb, literal_secret=token, ignore_dirs=getattr(cfg, "secret_scan_ignore_dirs", None))
    # Ignore the token file itself if explicitly found as literal. It is expected to contain the token.
    filtered = []
    token_path = str(tf).lower()
    for h in hits:
        if h.path.lower() == token_path:
            continue
        filtered.append(h)
    def _hit_to_dict(h):
        # Works with slots=True dataclasses. Never use h.__dict__.
        if is_dataclass(h):
            try:
                return asdict(h)
            except TypeError:
                pass
        return {
            "path": str(getattr(h, "path", "")),
            "line": int(getattr(h, "line", 0) or 0),
            "pattern": str(getattr(h, "pattern", "")),
            "preview": str(getattr(h, "preview", "")),
        }
    evidence = {"hit_count": len(filtered), "hits": [_hit_to_dict(h) for h in filtered[:100]]}
    if filtered:
        return Finding("", "", Status.FAIL, Severity.HIGH, "Potential secret material was found outside the token file.", evidence, "Remove secrets from logs/configs or rotate affected tokens.")
    return Finding("", "", Status.PASS, Severity.INFO, "No obvious secret leaks found in scanned project files.", evidence)


@timed("PERSIST-001", "v1.22d persistent vector JSONL ledger audit", "persistence")
def check_persistent_vector_ledger(cfg: SuiteConfig) -> Finding:
    p = Path(cfg.mycelia_root) / "state" / "smql_vector_index_v122d.jsonl"
    audit = audit_jsonl(str(p))
    if not audit.exists:
        return Finding("", "", Status.WARN, Severity.HIGH, "v1.22d persistence ledger does not exist.", audit.to_dict())
    if audit.events_failed:
        return Finding("", "", Status.FAIL, Severity.HIGH, "v1.22d persistence ledger has malformed events.", audit.to_dict())
    if not audit.latest_counts or audit.latest_counts.get(cfg.collection, 0) <= 0:
        return Finding("", "", Status.WARN, Severity.MEDIUM, "v1.22d persistence ledger exists but configured collection has no latest records.", audit.to_dict())
    return Finding("", "", Status.PASS, Severity.INFO, "v1.22d persistence ledger is present and parseable.", audit.to_dict())


@timed("RAM-PROBE-READY-001", "v1.22c vector RAM probe tool readiness", "memory")
def check_ram_probe_readiness(cfg: SuiteConfig) -> Finding:
    tool = Path(cfg.mycelia_root) / "tools" / "mycelia_memory_probe.py"
    if not tool.exists():
        return Finding("", "", Status.WARN, Severity.MEDIUM, "v1.22c memory probe tool is not installed.", {"path": str(tool)})
    p = subprocess.run([sys.executable, "-m", "py_compile", str(tool)], text=True, capture_output=True)
    if p.returncode != 0:
        return Finding("", "", Status.FAIL, Severity.HIGH, "v1.22c memory probe tool failed py_compile.", {"stderr": p.stderr})
    return Finding("", "", Status.PASS, Severity.INFO, "v1.22c memory probe tool is installed and syntactically valid.", {"path": str(tool)})


@timed("RAM-PROBE-LIVE-001", "Live vector RAM probe orchestration", "memory")
def check_live_ram_probe(cfg: SuiteConfig) -> Finding:
    if not cfg.run_live_ram_probe:
        return Finding("", "", Status.SKIP, Severity.INFO, "Live RAM probe not requested.", {})
    if not cfg.mycelia_pid:
        return Finding("", "", Status.SKIP, Severity.INFO, "Live RAM probe requested but no MyceliaDB PID was provided.", {})
    tool = Path(cfg.mycelia_root) / "tools" / "mycelia_memory_probe.py"
    if not tool.exists():
        return Finding("", "", Status.FAIL, Severity.HIGH, "Live RAM probe requested but probe tool is missing.", {"path": str(tool)})
    out = Path(cfg.reports_dir) / "live_vector_ram_probe.json"
    cmd = [
        sys.executable, str(tool),
        "--pid", str(cfg.mycelia_pid),
        "--during-smql-vector-search",
        "--query-text", "Was ist SMQL?",
        "--lmstudio-url", cfg.lmstudio_url,
        "--embedding-model", cfg.embedding_model,
        "--adapter-vault", str(Path(cfg.adapter_root) / ".smql_adapter"),
        "--collection", cfg.collection,
        "--max-vault-vectors", "50",
        "--during-command", str(Path(cfg.adapter_root) / "scripts" / "probe_during_smql.cmd"),
        "--during-command-cwd", cfg.adapter_root,
        "--scan-duration-seconds", str(cfg.ram_probe_seconds),
        "--json-out", str(out),
    ]
    p = subprocess.run(cmd, text=True, capture_output=True, timeout=max(30, cfg.ram_probe_seconds + 25))
    evidence: dict[str, Any] = {"returncode": p.returncode, "stdout": p.stdout[-1000:], "stderr": p.stderr[-1000:], "report": str(out)}
    if out.exists():
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
            evidence["probe_report"] = data
            vsp = data.get("vector_search_probe") or data
            if vsp.get("strict_no_cpu_ram_external_probe_passed") is True or vsp.get("vector_fragment_hits") == 0:
                return Finding("", "", Status.PASS, Severity.INFO, "Live vector RAM probe did not observe vector fragments.", evidence)
            return Finding("", "", Status.FAIL, Severity.CRITICAL, "Live vector RAM probe observed possible vector fragments.", evidence)
        except Exception as e:
            evidence["parse_error"] = repr(e)
    return Finding("", "", Status.FAIL, Severity.HIGH, "Live RAM probe did not produce a usable report.", evidence)


ALL_CHECKS = [
    check_adapter_health,
    check_lmstudio_models,
    check_mycelia_token_boundary,
    check_mycelia_status,
    check_web_chat_api_json,
    check_rag_chat_baseline,
    check_rag_redteam,
    check_secret_scan,
    check_persistent_vector_ledger,
    check_ram_probe_readiness,
    check_live_ram_probe,
]
