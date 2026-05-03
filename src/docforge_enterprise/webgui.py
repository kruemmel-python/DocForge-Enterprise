from __future__ import annotations

import html
import json
import os
import secrets
import shlex
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


DEFAULT_CHAT_MODEL = "google_gemma-4-e4b-it"
DEFAULT_EMBEDDING_MODEL = "text-embedding-nomic-embed-text-v2-moe"


@dataclass(slots=True)
class WebJob:
    id: str
    status: str = "queued"  # queued | running | success | failed
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    command: list[str] = field(default_factory=list)
    cwd: str = ""
    workspace: str = ""
    input_path: str = ""
    mode: str = ""
    returncode: int | None = None
    log: list[str] = field(default_factory=list)
    error: str = ""
    output_markdown: str = ""
    output_html: str = ""
    output_metadata: str = ""

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["duration_seconds"] = round((self.finished_at or time.time()) - (self.started_at or self.created_at), 3)
        data["command_display"] = " ".join(shlex.quote(part) for part in self.command)
        return data


class JobRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.jobs: dict[str, WebJob] = {}

    def create(self) -> WebJob:
        job = WebJob(id=uuid.uuid4().hex[:12])
        with self._lock:
            self.jobs[job.id] = job
        return job

    def get(self, job_id: str) -> WebJob | None:
        with self._lock:
            return self.jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.public() for job in sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)]

    def append_log(self, job: WebJob, line: str) -> None:
        with self._lock:
            job.log.append(line.rstrip("\n"))
            if len(job.log) > 5000:
                job.log = job.log[-5000:]


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _safe_filename(name: str) -> str:
    name = Path(name or "upload.zip").name
    clean = "".join(ch if ch.isalnum() or ch in ".-_ " else "_" for ch in name).strip()
    return clean or "upload.zip"


def build_command(payload: dict[str, Any], input_path: Path, workspace: Path) -> list[str]:
    mode = str(payload.get("mode") or "embedded_mycelia")
    cmd = [
        sys.executable,
        "-m",
        "docforge_enterprise.cli",
        str(input_path),
        "--workspace",
        str(workspace),
        "--chat-model",
        str(payload.get("chat_model") or DEFAULT_CHAT_MODEL),
        "--embedding-model",
        str(payload.get("embedding_model") or DEFAULT_EMBEDDING_MODEL),
        "--analysis-workers",
        str(_int(payload.get("analysis_workers"), 1)),
        "--chat-timeout",
        str(_float(payload.get("chat_timeout"), 600.0)),
        "--embedding-timeout",
        str(_float(payload.get("embedding_timeout"), 300.0)),
        "--gateway-timeout",
        str(_float(payload.get("gateway_timeout"), 180.0)),
        "--final-timeout",
        str(_float(payload.get("final_timeout"), 600.0)),
        "--max-chars-per-shard",
        str(_int(payload.get("max_chars_per_shard"), 2500)),
        "--max-embedding-batch-size",
        str(_int(payload.get("max_embedding_batch_size"), 4)),
        "--analysis-max-tokens",
        str(_int(payload.get("analysis_max_tokens"), 900)),
        "--chapter-max-tokens",
        str(_int(payload.get("chapter_max_tokens"), 3500)),
        "--llm-retries",
        str(_int(payload.get("llm_retries"), 3)),
        "--retry-backoff",
        str(_float(payload.get("retry_backoff"), 2.0)),
        "--profile",
        str(payload.get("profile") or "enterprise"),
    ]

    chapters = str(payload.get("chapters") or "").strip()
    if chapters:
        cmd += ["--chapters", chapters]
    max_final_chapters = _int(payload.get("max_final_chapters"), 0)
    if max_final_chapters > 0:
        cmd += ["--max-final-chapters", str(max_final_chapters)]
    if _bool(payload.get("single_pass_final", False)):
        cmd.append("--single-pass-final")
    if _bool(payload.get("disable_module_reduce", False)):
        cmd.append("--disable-module-reduce")
    if _bool(payload.get("estimate_only", False)):
        cmd.append("--estimate-only")

    lmstudio_url = str(payload.get("lmstudio_url") or "").strip()
    if lmstudio_url:
        cmd += ["--lmstudio-url", lmstudio_url]

    project_name = str(payload.get("project_name") or "").strip()
    if project_name:
        cmd += ["--project-name", project_name]

    retrieval_limit = _int(payload.get("retrieval_limit"), 8)
    if retrieval_limit > 0:
        cmd += ["--retrieval-limit", str(retrieval_limit)]

    max_analysis_workers = _int(payload.get("max_analysis_workers"), 2)
    if max_analysis_workers > 0:
        cmd += ["--max-analysis-workers", str(max_analysis_workers)]

    mycelia_url = str(payload.get("mycelia_url") or "").strip()
    if mycelia_url:
        cmd += ["--mycelia-url", mycelia_url]

    mycelia_token = str(payload.get("mycelia_token") or "").strip()
    if mycelia_token:
        cmd += ["--mycelia-token", mycelia_token]

    if _bool(payload.get("force_rebuild", True)):
        cmd.append("--force-rebuild")
    if _bool(payload.get("no_adaptive_shard", False)):
        cmd.append("--no-adaptive-shard")
    if _bool(payload.get("fail_on_timeout", False)):
        cmd.append("--fail-on-timeout")

    match mode:
        case "dry_run":
            cmd.append("--dry-run")
        case "embedded_mycelia":
            cmd.append("--embedded-mycelia")
            port = _int(payload.get("embedded_mycelia_port"), 9999)
            cmd += ["--embedded-mycelia-port", str(port)]
        case "sidecar_lmstudio":
            cmd.append("--sidecar-only")
        case "external_mycelia":
            # Use configured --mycelia-url or default settings.
            pass
        case _:
            cmd.append("--embedded-mycelia")

    return cmd


def run_job(registry: JobRegistry, job: WebJob, payload: dict[str, Any], input_path: Path, workspace: Path) -> None:
    with registry._lock:
        job.status = "running"
        job.started_at = time.time()
        job.input_path = str(input_path)
        job.workspace = str(workspace)
        job.mode = str(payload.get("mode") or "embedded_mycelia")
        job.command = build_command(payload, input_path, workspace)
        job.cwd = str(Path.cwd())

    env = os.environ.copy()
    token = str(payload.get("mycelia_token") or "").strip()
    if token:
        env["MYCELIA_LOCAL_TOKEN"] = token

    registry.append_log(job, "Starting DocForge Enterprise job.")
    registry.append_log(job, "Command: " + " ".join(shlex.quote(part) for part in job.command))

    try:
        proc = subprocess.Popen(
            job.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            registry.append_log(job, line)
        rc = proc.wait()
        with registry._lock:
            job.returncode = rc
            job.finished_at = time.time()
            if rc == 0:
                job.status = "success"
            else:
                job.status = "failed"
                job.error = f"DocForge process exited with code {rc}."

        output_dir = workspace / "output"
        md = output_dir / "enterprise_documentation.md"
        html_path = output_dir / "enterprise_documentation.html"
        metadata = output_dir / "run_metadata.json"

        with registry._lock:
            if md.exists():
                job.output_markdown = str(md)
            if html_path.exists():
                job.output_html = str(html_path)
            if metadata.exists():
                job.output_metadata = str(metadata)

        if rc == 0:
            registry.append_log(job, "DocForge Enterprise finished successfully.")
        else:
            registry.append_log(job, f"DocForge Enterprise failed with return code {rc}.")
    except Exception as exc:  # noqa: BLE001 - web UI must surface any launch error
        with registry._lock:
            job.status = "failed"
            job.error = str(exc)
            job.finished_at = time.time()
        registry.append_log(job, f"Launcher error: {exc}")



def _parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Small multipart/form-data parser for local WebGUI uploads.

    It intentionally supports the simple browser form generated by this module:
    text fields plus one optional file upload. This avoids the removed `cgi`
    module on Python 3.13+ while keeping the project dependency-free.
    """
    marker = "boundary="
    if marker not in content_type:
        raise ValueError("missing multipart boundary")
    boundary = content_type.split(marker, 1)[1].split(";", 1)[0].strip().strip('"')
    if not boundary:
        raise ValueError("empty multipart boundary")

    delimiter = ("--" + boundary).encode("utf-8")
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}

    for part in body.split(delimiter):
        part = part.strip()
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].strip()
        if b"\r\n\r\n" in part:
            raw_headers, data = part.split(b"\r\n\r\n", 1)
            newline = b"\r\n"
        elif b"\n\n" in part:
            raw_headers, data = part.split(b"\n\n", 1)
            newline = b"\n"
        else:
            continue
        if data.endswith(newline):
            data = data[: -len(newline)]

        headers = raw_headers.decode("utf-8", errors="replace").splitlines()
        disposition = ""
        for header in headers:
            if header.lower().startswith("content-disposition:"):
                disposition = header.split(":", 1)[1].strip()
                break
        if not disposition:
            continue

        attrs: dict[str, str] = {}
        for item in disposition.split(";"):
            item = item.strip()
            if "=" in item:
                key, value = item.split("=", 1)
                attrs[key.strip().lower()] = value.strip().strip('"')
        name = attrs.get("name", "")
        if not name:
            continue
        filename = attrs.get("filename")
        if filename is not None and filename != "":
            files[name] = (filename, data)
        else:
            fields[name] = data.decode("utf-8", errors="replace")

    return fields, files


INDEX_HTML = r"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>DocForge Enterprise WebGUI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root { color-scheme: light dark; }
    body { font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 0; background: #0f172a; color: #e5e7eb; }
    header { padding: 22px 28px; background: linear-gradient(135deg, #111827, #1e293b); border-bottom: 1px solid #334155; }
    h1 { margin: 0; font-size: 24px; }
    main { display: grid; grid-template-columns: 420px 1fr; gap: 18px; padding: 18px; }
    section { background: #111827; border: 1px solid #334155; border-radius: 16px; padding: 16px; box-shadow: 0 12px 30px rgba(0,0,0,.22); }
    label { display: block; margin: 10px 0 4px; font-size: 13px; color: #cbd5e1; }
    input, select, textarea, button { box-sizing: border-box; width: 100%; border-radius: 10px; border: 1px solid #475569; background: #020617; color: #e5e7eb; padding: 9px 10px; }
    button { cursor: pointer; background: #2563eb; border-color: #3b82f6; font-weight: 700; margin-top: 12px; }
    button.secondary { background: #334155; border-color: #475569; }
    .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .hint { color: #94a3b8; font-size: 12px; line-height: 1.45; }
    .status { display: inline-block; padding: 4px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }
    .queued { background: #475569; }
    .running { background: #0369a1; }
    .success { background: #15803d; }
    .failed { background: #b91c1c; }
    pre { white-space: pre-wrap; background: #020617; border: 1px solid #334155; border-radius: 12px; padding: 12px; max-height: 320px; overflow: auto; }
    iframe { width: 100%; height: 650px; border: 1px solid #334155; border-radius: 12px; background: white; }
    .tabs { display: flex; gap: 8px; margin-bottom: 10px; }
    .tabs button { width: auto; margin: 0; padding: 8px 12px; }
    .row { display: flex; align-items: center; gap: 8px; }
    .row input[type=checkbox] { width: auto; }
    @media (max-width: 980px) { main { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
<header>
  <h1>DocForge Enterprise WebGUI</h1>
  <div class="hint">Lokale Oberfläche für Enterprise-Dokumentation mit LM Studio, Embedded MyceliaDB, Sidecar-Vectorstore oder Dry-Run.</div>
</header>
<main>
  <section>
    <h2>Generierung starten</h2>
    <form id="jobForm">
      <label>Projektdatei hochladen (.zip oder .md)</label>
      <input type="file" name="upload" accept=".zip,.md,.txt">

      <label>Oder lokaler Pfad auf dem Server</label>
      <input name="input_path" placeholder="D:\docforge_enterprise_new\examples\sample_project.zip">

      <label>Ausführungsmodus</label>
      <select name="mode">
        <option value="embedded_mycelia">Mit LM Studio + Embedded MyceliaDB</option>
        <option value="sidecar_lmstudio">Mit LM Studio + Sidecar-Vectorstore</option>
        <option value="external_mycelia">Mit externer MyceliaDB-URL</option>
        <option value="dry_run">Dry-Run ohne LLM</option>
      </select>

      <label>Dokumentationsprofil</label>
      <select name="profile">
        <option value="quick">Quick — wenige LLM-Calls, Single-Pass-Finale</option>
        <option value="balanced">Balanced — reduzierte Kapitel, Module bleiben erhalten</option>
        <option value="enterprise" selected>Enterprise — vollständige Kapitelpipeline</option>
      </select>
      <div class="hint">
        Quick ist ideal für Samples und kleine Projekte. Enterprise rendert jedes Kapitel separat und erzeugt deshalb deutlich mehr LLM-Aufrufe.
      </div>

      <label>Kapitel optional, kommagetrennt</label>
      <textarea name="chapters" rows="2" placeholder="Executive Summary,Systemüberblick,Sicherheitsbetrachtung"></textarea>

      <div class="grid2">
        <div><label>Max. finale Kapitel</label><input name="max_final_chapters" value="0"></div>
        <div><label>Planung</label><div class="hint" style="padding-top:10px">0 = kein Limit</div></div>
      </div>

      <div class="row"><input type="checkbox" name="single_pass_final"><label>Finale Doku als Single-Pass rendern</label></div>
      <div class="row"><input type="checkbox" name="disable_module_reduce"><label>LLM-Modul-Reduktion überspringen</label></div>
      <div class="row"><input type="checkbox" name="estimate_only"><label>Nur LLM-/Embedding-Aufwand schätzen</label></div>

      <label>Projektname optional</label>
      <input name="project_name" placeholder="Mein Enterprise Projekt">

      <label>Mycelia Local Token</label>
      <div class="grid2">
        <input name="mycelia_token" id="token" placeholder="Token einfügen oder generieren">
        <button type="button" class="secondary" onclick="generateToken()">Token generieren</button>
      </div>
      <div class="hint">Temporär pro Terminal/WebGUI-Prozess. Dauerhaft unter Windows z. B.: <code>[Environment]::SetEnvironmentVariable("MYCELIA_LOCAL_TOKEN","TOKEN","User")</code></div>

      <label>LM Studio Chat-Modell</label>
      <input name="chat_model" value="google_gemma-4-e4b-it">

      <label>LM Studio Embedding-Modell</label>
      <input name="embedding_model" value="text-embedding-nomic-embed-text-v2-moe">

      <label>LM Studio URL</label>
      <input name="lmstudio_url" value="http://127.0.0.1:1234/v1">

      <label>MyceliaDB URL</label>
      <input name="mycelia_url" value="http://127.0.0.1:9999">

      <div class="grid2">
        <div>
          <label>Embedded Mycelia Port</label>
          <input name="embedded_mycelia_port" value="9999">
        </div>
        <div>
          <label>Retrieval Top-K</label>
          <input name="retrieval_limit" value="8">
        </div>
      </div>

      <h3>Timeouts & Skalierung</h3>
      <div class="grid2">
        <div><label>Analysis Workers</label><input name="analysis_workers" value="1"></div>
        <div><label>Max Workers</label><input name="max_analysis_workers" value="2"></div>
        <div><label>Chat Timeout</label><input name="chat_timeout" value="600"></div>
        <div><label>Embedding Timeout</label><input name="embedding_timeout" value="300"></div>
        <div><label>Gateway Timeout</label><input name="gateway_timeout" value="180"></div>
        <div><label>Final Timeout</label><input name="final_timeout" value="600"></div>
        <div><label>Shard-Größe</label><input name="max_chars_per_shard" value="2500"></div>
        <div><label>Embedding Batch</label><input name="max_embedding_batch_size" value="4"></div>
        <div><label>Analyse Tokens</label><input name="analysis_max_tokens" value="900"></div>
        <div><label>Kapitel Tokens</label><input name="chapter_max_tokens" value="3500"></div>
        <div><label>LLM Retries</label><input name="llm_retries" value="3"></div>
        <div><label>Retry Backoff</label><input name="retry_backoff" value="2.0"></div>
      </div>

      <div class="row"><input type="checkbox" name="force_rebuild" checked><label>Force Rebuild</label></div>
      <div class="row"><input type="checkbox" name="no_adaptive_shard"><label>Adaptive Shard-Rettung deaktivieren</label></div>
      <div class="row"><input type="checkbox" name="fail_on_timeout"><label>Bei Timeout abbrechen statt Fallback schreiben</label></div>

      <button type="submit">Dokumentation erstellen</button>
    </form>
  </section>

  <section>
    <h2>Status</h2>
    <div id="current">Noch kein Job gestartet.</div>
    <div class="tabs">
      <button type="button" onclick="showTab('log')">Logs</button>
      <button type="button" onclick="showTab('doc')">Dokumentation</button>
      <button type="button" onclick="showTab('jobs')">Jobs</button>
    </div>
    <div id="tab-log"><pre id="logs"></pre></div>
    <div id="tab-doc" style="display:none">
      <div id="docLinks" class="hint"></div>
      <iframe id="docFrame"></iframe>
    </div>
    <div id="tab-jobs" style="display:none"><pre id="jobs"></pre></div>
  </section>
</main>
<script>
let currentJob = null;
let activeTab = "log";

function showTab(name) {
  activeTab = name;
  for (const tab of ["log", "doc", "jobs"]) {
    document.getElementById("tab-" + tab).style.display = tab === name ? "block" : "none";
  }
}

async function generateToken() {
  const res = await fetch("/api/token", {method: "POST"});
  const data = await res.json();
  document.getElementById("token").value = data.token;
}

document.getElementById("jobForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = new FormData(ev.target);
  const res = await fetch("/api/start", {method: "POST", body: form});
  const data = await res.json();
  if (!res.ok) {
    alert(data.error || "Start fehlgeschlagen");
    return;
  }
  currentJob = data.job_id;
  showTab("log");
  poll();
});

async function poll() {
  if (currentJob) {
    const res = await fetch("/api/job/" + currentJob);
    const job = await res.json();
    document.getElementById("current").innerHTML =
      `<span class="status ${job.status}">${job.status}</span> Job ${job.id} · Dauer ${job.duration_seconds}s` +
      (job.error ? `<br><b>Fehler:</b> ${escapeHtml(job.error)}` : "");
    document.getElementById("logs").textContent = (job.log || []).join("\n");
    if (job.status === "success" && job.output_html) {
      document.getElementById("docFrame").src = "/api/job/" + job.id + "/html";
      document.getElementById("docLinks").innerHTML =
        `<a href="/api/job/${job.id}/markdown" target="_blank">Markdown öffnen</a> · ` +
        `<a href="/api/job/${job.id}/metadata" target="_blank">Metadaten öffnen</a>`;
    }
  }
  const jobs = await (await fetch("/api/jobs")).json();
  document.getElementById("jobs").textContent = JSON.stringify(jobs, null, 2);
  setTimeout(poll, 1500);
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
poll();
</script>
</body>
</html>
"""


class WebGUIHandler(BaseHTTPRequestHandler):
    registry: JobRegistry

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep terminal output clean; job logs are shown in the UI.
        return

    def _json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _text(self, text: str, *, content_type: str = "text/plain; charset=utf-8", status: int = 200) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._text(INDEX_HTML, content_type="text/html; charset=utf-8")
            return
        if path == "/api/jobs":
            self._json(self.registry.list())
            return
        if path.startswith("/api/job/"):
            parts = path.strip("/").split("/")
            job = self.registry.get(parts[2]) if len(parts) >= 3 else None
            if job is None:
                self._json({"error": "job not found"}, status=404)
                return
            if len(parts) == 3:
                self._json(job.public())
                return
            target = parts[3]
            if target == "html" and job.output_html:
                p = Path(job.output_html)
                if p.exists():
                    self._text(p.read_text(encoding="utf-8"), content_type="text/html; charset=utf-8")
                    return
            if target == "markdown" and job.output_markdown:
                p = Path(job.output_markdown)
                if p.exists():
                    self._text(p.read_text(encoding="utf-8"), content_type="text/markdown; charset=utf-8")
                    return
            if target == "metadata" and job.output_metadata:
                p = Path(job.output_metadata)
                if p.exists():
                    self._text(p.read_text(encoding="utf-8"), content_type="application/json; charset=utf-8")
                    return
            self._json({"error": "artifact not available"}, status=404)
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/token":
            self._json({"token": secrets.token_urlsafe(32)})
            return
        if parsed.path == "/api/start":
            self._handle_start()
            return
        self._json({"error": "not found"}, status=404)

    def _handle_start(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._json({"error": "expected multipart/form-data"}, status=400)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            payload, files = _parse_multipart(body, content_type)
        except Exception as exc:
            self._json({"error": f"multipart parsing failed: {exc}"}, status=400)
            return

        for checkbox in ("force_rebuild", "no_adaptive_shard", "fail_on_timeout", "single_pass_final", "disable_module_reduce", "estimate_only"):
            payload[checkbox] = checkbox in payload

        job = self.registry.create()
        job_dir = self.registry.root / "jobs" / job.id
        upload_dir = job_dir / "input"
        workspace = job_dir / "workspace"
        upload_dir.mkdir(parents=True, exist_ok=True)

        input_path_value = str(payload.get("input_path") or "").strip()
        input_path: Path | None = Path(input_path_value) if input_path_value else None

        if "upload" in files:
            filename, data = files["upload"]
            filename = _safe_filename(filename)
            input_path = upload_dir / filename
            input_path.write_bytes(data)

        if input_path is None or not input_path.exists():
            self._json({"error": "Bitte Upload-Datei oder existierenden lokalen input_path angeben."}, status=400)
            return

        thread = threading.Thread(
            target=run_job,
            args=(self.registry, job, payload, input_path, workspace),
            name=f"docforge-web-job-{job.id}",
            daemon=True,
        )
        thread.start()
        self._json({"job_id": job.id, "status": job.status})


def serve(host: str = "127.0.0.1", port: int = 7860, root: Path | None = None) -> None:
    registry = JobRegistry(root or Path(".docforge_webgui"))
    WebGUIHandler.registry = registry
    server = ThreadingHTTPServer((host, port), WebGUIHandler)
    print(f"DocForge Enterprise WebGUI running on http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="docforge-webgui",
        description="Start the local DocForge Enterprise WebGUI.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--root", type=Path, default=Path(".docforge_webgui"))
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port, root=args.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
