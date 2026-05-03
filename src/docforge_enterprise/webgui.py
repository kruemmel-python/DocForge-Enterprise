from __future__ import annotations
import html, json, os, secrets, shlex, subprocess, sys, threading, time, uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from myceliadb_embedded.auth_store import MyceliaIdentityStore
DEFAULT_CHAT_MODEL="google_gemma-4-e4b-it"; DEFAULT_EMBEDDING_MODEL="text-embedding-nomic-embed-text-v2-moe"
@dataclass(slots=True)
class WebJob:
    id:str; status:str="queued"; created_at:float=field(default_factory=time.time); started_at:float|None=None; finished_at:float|None=None
    command:list[str]=field(default_factory=list); workspace:str=""; input_path:str=""; returncode:int|None=None; log:list[str]=field(default_factory=list); error:str=""; output_markdown:str=""; output_html:str=""; output_metadata:str=""
    def public(self): 
        d=asdict(self); d["duration_seconds"]=round((self.finished_at or time.time())-(self.started_at or self.created_at),3); d["command_display"]=" ".join(shlex.quote(x) for x in self.command); return d
class Registry:
    def __init__(self,root:Path): self.root=root; root.mkdir(parents=True,exist_ok=True); self.jobs={}; self.lock=threading.RLock(); self.auth=MyceliaIdentityStore(root/"mycelia_auth")
    def create(self): 
        j=WebJob(uuid.uuid4().hex[:12]); self.jobs[j.id]=j; return j
    def get(self,i): return self.jobs.get(i)
    def list(self): return [j.public() for j in sorted(self.jobs.values(),key=lambda j:j.created_at,reverse=True)]
    def log(self,j,line):
        with self.lock: j.log.append(line.rstrip()); j.log=j.log[-5000:]
def _parse_cookies(h): 
    out={}
    for part in (h or "").split(";"):
        if "=" in part:
            k,v=part.strip().split("=",1); out[k]=v
    return out
def _form_urlencoded(body): return {k:v[-1] for k,v in parse_qs(body.decode(),keep_blank_values=True).items()}
def _bool(v): return str(v or "").lower() in {"1","true","yes","on"}
def _safe_filename(n): return "".join(c if c.isalnum() or c in ".-_" else "_" for c in Path(n or "upload.zip").name)
def _parse_multipart(body:bytes,ct:str,max_upload:int):
    b="boundary="
    if b not in ct: raise ValueError("missing boundary")
    boundary=ct.split(b,1)[1].split(";",1)[0].strip().strip('"').encode()
    fields={}; files={}
    for part in body.split(b"--"+boundary):
        part=part.strip()
        if not part or part==b"--": continue
        if part.endswith(b"--"): part=part[:-2].strip()
        if b"\r\n\r\n" in part: hdr,data=part.split(b"\r\n\r\n",1); nl=b"\r\n"
        elif b"\n\n" in part: hdr,data=part.split(b"\n\n",1); nl=b"\n"
        else: continue
        if data.endswith(nl): data=data[:-len(nl)]
        disp=""
        for line in hdr.decode("utf-8","replace").splitlines():
            if line.lower().startswith("content-disposition:"): disp=line.split(":",1)[1]
        attrs={}
        for x in disp.split(";"):
            if "=" in x:
                k,v=x.strip().split("=",1); attrs[k.lower()]=v.strip().strip('"')
        name=attrs.get("name",""); fn=attrs.get("filename")
        if not name: continue
        if fn:
            if len(data)>max_upload: raise ValueError("upload too large")
            files[name]=(_safe_filename(fn),data)
        else: fields[name]=data.decode("utf-8","replace")
    return fields,files
def _val(payload:dict, key:str, default:str="") -> str:
    v = payload.get(key, default)
    return str(default if v is None or v == "" else v)

def build_command(payload,input_path:Path,workspace:Path):
    cmd=[
        sys.executable,"-m","docforge_enterprise.cli",
        str(input_path),
        "--workspace",str(workspace),
        "--profile",_val(payload,"profile","balanced"),
        "--language",_val(payload,"language","de"),
        "--chat-model",_val(payload,"chat_model",DEFAULT_CHAT_MODEL),
        "--embedding-model",_val(payload,"embedding_model",DEFAULT_EMBEDDING_MODEL),
        "--analysis-workers",_val(payload,"analysis_workers","1"),
        "--max-analysis-workers",_val(payload,"max_analysis_workers","2"),
        "--chat-timeout",_val(payload,"chat_timeout","600"),
        "--embedding-timeout",_val(payload,"embedding_timeout","300"),
        "--gateway-timeout",_val(payload,"gateway_timeout","180"),
        "--final-timeout",_val(payload,"final_timeout","600"),
        "--max-chars-per-shard",_val(payload,"max_chars_per_shard","2500"),
        "--max-embedding-batch-size",_val(payload,"max_embedding_batch_size","4"),
        "--analysis-max-tokens",_val(payload,"analysis_max_tokens","900"),
        "--chapter-max-tokens",_val(payload,"chapter_max_tokens","3500"),
        "--llm-retries",_val(payload,"llm_retries","3"),
        "--retry-backoff",_val(payload,"retry_backoff","2.0"),
    ]
    if payload.get("lmstudio_url"): cmd += ["--lmstudio-url", payload["lmstudio_url"]]
    if payload.get("project_name"): cmd += ["--project-name", payload["project_name"]]
    if payload.get("chapters"): cmd += ["--chapters", payload["chapters"]]
    if payload.get("retrieval_limit"): cmd += ["--retrieval-limit", payload["retrieval_limit"]]
    if payload.get("max_final_chapters") and payload.get("max_final_chapters") != "0":
        cmd += ["--max-final-chapters", payload["max_final_chapters"]]
    if payload.get("mycelia_url"): cmd += ["--mycelia-url", payload["mycelia_url"]]
    if payload.get("mycelia_token"): cmd += ["--mycelia-token", payload["mycelia_token"]]
    if payload.get("embedded_mycelia_port"): cmd += ["--embedded-mycelia-port", payload["embedded_mycelia_port"]]
    for flag in ["single_pass_final","disable_module_reduce","estimate_only","force_rebuild","no_adaptive_shard","fail_on_timeout","allow_missing_shard_analyses"]:
        if _bool(payload.get(flag)): cmd.append("--"+flag.replace("_","-"))
    mode=payload.get("mode","embedded_mycelia")
    if mode=="dry_run":
        cmd.append("--dry-run")
    elif mode=="sidecar_lmstudio":
        cmd.append("--sidecar-only")
    elif mode=="embedded_mycelia":
        cmd.append("--embedded-mycelia")
    elif mode=="external_mycelia":
        pass
    else:
        cmd.append("--embedded-mycelia")
    return cmd
def run_job(reg,j,payload,input_path,workspace,actor):
    with reg.lock:
        j.status="running"; j.started_at=time.time(); j.input_path=str(input_path); j.workspace=str(workspace); j.command=build_command(payload,input_path,workspace)
    reg.log(j, f"[WebGUI] Job {j.id} gestartet.")
    reg.log(j, f"[WebGUI] Eingabe: {input_path}")
    reg.log(j, f"[WebGUI] Workspace: {workspace}")
    reg.log(j, "[WebGUI] Command: " + " ".join(shlex.quote(x) for x in j.command))
    reg.auth.audit(actor,"job_start",j.id,metadata=json.dumps({"cmd":j.command}))
    env=os.environ.copy()
    if payload.get("mycelia_token"): env["MYCELIA_LOCAL_TOKEN"]=payload["mycelia_token"]
    try:
        p=subprocess.Popen(j.command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",errors="replace",env=env,bufsize=1)
        assert p.stdout
        for line in p.stdout:
            reg.log(j,line)
        rc=p.wait()
        with reg.lock: j.returncode=rc; j.finished_at=time.time(); j.status="success" if rc==0 else "failed"; j.error="" if rc==0 else f"exit code {rc}"
        out=workspace/"output"; 
        for attr,name in [("output_markdown","enterprise_documentation.md"),("output_html","enterprise_documentation.html"),("output_metadata","run_metadata.json")]:
            path=out/name
            if path.exists(): setattr(j,attr,str(path))
        reg.auth.audit(actor,"job_finish",j.id,metadata=json.dumps({"rc":rc}))
    except Exception as e:
        with reg.lock: j.status="failed"; j.error=str(e); j.finished_at=time.time()
        reg.auth.audit(actor,"job_error",j.id,metadata=str(e))
HTML = r"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>DocForge Enterprise Secure WebGUI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root{
      --bg:#0b1220; --panel:#111827; --panel2:#0f172a; --line:#334155;
      --text:#e5e7eb; --muted:#94a3b8; --accent:#2563eb; --accent2:#3b82f6;
      --ok:#16a34a; --bad:#dc2626; --warn:#d97706;
    }
    *{box-sizing:border-box}
    body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--bg);color:var(--text);font-size:14px}
    header{padding:22px 28px;background:linear-gradient(135deg,#111827,#1e293b);border-bottom:1px solid var(--line)}
    h1{margin:0;font-size:24px}
    h2{margin:0 0 12px;font-size:18px}
    h3{margin:18px 0 8px;font-size:15px;color:#dbeafe}
    .hint{color:var(--muted);font-size:12px;line-height:1.45}
    .layout{display:grid;grid-template-columns:430px 1fr;gap:18px;padding:18px}
    section{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px;box-shadow:0 12px 30px rgba(0,0,0,.22)}
    label{display:block;margin:10px 0 4px;font-size:13px;color:#cbd5e1}
    input,select,textarea,button{width:100%;border-radius:10px;border:1px solid #475569;background:#020617;color:var(--text);padding:9px 10px}
    textarea{min-height:62px;resize:vertical}
    button{cursor:pointer;background:var(--accent);border-color:var(--accent2);font-weight:700;margin-top:10px}
    button.secondary{background:#334155;border-color:#475569}
    button.small{width:auto;margin:0;padding:7px 10px}
    .grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
    .row{display:flex;align-items:center;gap:8px;margin:8px 0}
    .row input[type=checkbox]{width:auto}
    .tabs{display:flex;gap:8px;margin:10px 0}
    .tabs button{width:auto;margin:0;padding:8px 12px}
    .status{display:inline-block;padding:4px 8px;border-radius:999px;font-size:12px;font-weight:800;background:#475569}
    .running{background:#0369a1}.success{background:#15803d}.failed{background:#b91c1c}.queued{background:#475569}
    pre{white-space:pre-wrap;background:#020617;border:1px solid var(--line);border-radius:12px;padding:12px;max-height:330px;overflow:auto}
    iframe{width:100%;height:650px;border:1px solid var(--line);border-radius:12px;background:white}
    #authBar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 18px;background:#0f172a;border-bottom:1px solid var(--line)}
    #loginBox{max-width:520px;margin:32px auto}
    .pill{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:4px 9px;color:#cbd5e1;background:#020617}
    .disabled{opacity:.55;pointer-events:none}
    @media(max-width:1050px){.layout{grid-template-columns:1fr}.grid2,.grid3{grid-template-columns:1fr}}
  </style>
</head>
<body>
<header>
  <h1>DocForge Enterprise Secure WebGUI</h1>
  <div class="hint">Lokale Enterprise-Dokumentation mit LM Studio, Embedded MyceliaDB, Sidecar-Vectorstore, Dry-Run, Auth, CSRF und Audit-Log.</div>
</header>

<div id="authBar">
  <div><select id="uiLang" class="small" onchange="setLanguage(this.value, true)"><option value="de">DE · Deutsch</option><option value="en">EN · English</option></select></div><div id="authInfo" class="hint">Session wird geprüft ...</div>
  <div><button id="logoutBtn" class="small secondary" onclick="logout()" style="display:none">Logout</button></div>
</div>

<section id="loginBox" style="display:none">
  <h2>Login / Registrierung</h2>
  <div class="hint">Beim ersten Benutzer wird automatisch die Rolle <b>admin</b> vergeben. Danach einloggen und Jobs starten.</div>
  <label>Benutzername</label>
  <input id="loginUser" autocomplete="username" placeholder="admin">
  <label>Passwort</label>
  <input id="loginPass" type="password" autocomplete="current-password" placeholder="Sicheres Passwort">
  <div class="grid2">
    <button onclick="login()">Login</button>
    <button class="secondary" onclick="register()">Registrieren</button>
  </div>
  <pre id="authMsg"></pre>
</section>

<main id="app" class="layout" style="display:none">
  <section>
    <h2>Generierung starten</h2>
    <form id="f" enctype="multipart/form-data">
      <input type="hidden" name="csrf" id="csrf">
      <input type="hidden" name="language" id="languageField" value="de">

      <label>Projektdatei hochladen (.zip oder .md)</label>
      <input type="file" name="upload" accept=".zip,.md,.txt">

      <label>Oder lokaler Pfad auf dem Server</label>
      <input name="input_path" value="D:\docforge_enterprise_new\examples\sample_project.zip">

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
        <option value="balanced" selected>Balanced — reduzierte Kapitel, Module bleiben erhalten</option>
        <option value="enterprise">Enterprise — vollständige Kapitelpipeline</option>
      </select>
      <div class="hint">Quick ist ideal für Samples. Enterprise rendert jedes Kapitel separat und erzeugt deutlich mehr LLM-Aufrufe.</div>

      <label>Kapitel optional, kommagetrennt</label>
      <textarea name="chapters" placeholder="Executive Summary,Systemüberblick,Sicherheitsbetrachtung"></textarea>

      <div class="grid2">
        <div><label>Max. finale Kapitel</label><input name="max_final_chapters" value="0"></div>
        <div><label>Retrieval Top-K</label><input name="retrieval_limit" value="8"></div>
      </div>

      <div class="row"><input type="checkbox" name="single_pass_final"><label>Finale Doku als Single-Pass rendern</label></div>
      <div class="row"><input type="checkbox" name="disable_module_reduce"><label>LLM-Modul-Reduktion überspringen</label></div>
      <div class="row"><input type="checkbox" name="estimate_only"><label>Nur LLM-/Embedding-Aufwand schätzen</label></div>

      <label>Projektname optional</label>
      <input name="project_name" placeholder="Mein Enterprise Projekt">

      <h3>Security / Mycelia</h3>
      <label>Mycelia Local Token</label>
      <div class="grid2">
        <input name="mycelia_token" id="tok" placeholder="Token einfügen oder generieren">
        <button type="button" class="secondary" onclick="gen()">Token generieren</button>
      </div>
      <div class="hint">Temporär pro WebGUI-Prozess. Dauerhaft: <code>[Environment]::SetEnvironmentVariable("MYCELIA_LOCAL_TOKEN","TOKEN","User")</code></div>

      <div class="grid2">
        <div><label>LM Studio Chat-Modell</label><input name="chat_model" value="google_gemma-4-e4b-it"></div>
        <div><label>LM Studio Embedding-Modell</label><input name="embedding_model" value="text-embedding-nomic-embed-text-v2-moe"></div>
      </div>

      <label>LM Studio URL</label>
      <input name="lmstudio_url" value="http://127.0.0.1:1234/v1">

      <label>MyceliaDB URL</label>
      <input name="mycelia_url" value="http://127.0.0.1:9999">

      <div class="grid2">
        <div><label>Embedded Mycelia Port</label><input name="embedded_mycelia_port" value="9999"></div>
        <div><label>Max Workers</label><input name="max_analysis_workers" value="2"></div>
      </div>

      <h3>Timeouts & Skalierung</h3>
      <div class="grid2">
        <div><label>Analysis Workers</label><input name="analysis_workers" value="1"></div>
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
      <div class="row"><input type="checkbox" name="allow_missing_shard_analyses"><label>Integritätsfehler nur als Fallback erlauben (nicht empfohlen)</label></div>
      <div class="hint">Standard: File-Reduce bricht ab, wenn keine Shard-Analysen gefunden werden. So werden leere Prompts wie „Keine Shard-Analysen verfügbar“ verhindert.</div>

      <button>Dokumentation erstellen</button>
    </form>
  </section>

  <section>
    <h2>Status</h2>
    <div id="current">Noch kein Job gestartet.</div>
    <div id="plan" class="hint"></div>
    <div class="tabs">
      <button type="button" onclick="showTab('log')">Logs</button>
      <button type="button" onclick="showTab('doc')">Dokumentation</button>
      <button type="button" onclick="showTab('jobs')">Jobs</button>
      <button type="button" onclick="showTab('meta')">Metadaten</button>
    </div>
    <div id="tab-log"><pre id="log"></pre></div>
    <div id="tab-doc" style="display:none">
      <div id="docLinks" class="hint"></div>
      <iframe id="doc"></iframe>
    </div>
    <div id="tab-jobs" style="display:none"><pre id="jobs"></pre></div>
    <div id="tab-meta" style="display:none"><pre id="meta"></pre></div>
  </section>
</main>

<script>

const I18N = {
  en: {
    "DocForge Enterprise Secure WebGUI":"DocForge Enterprise Secure WebGUI",
    "Lokale Enterprise-Dokumentation mit LM Studio, Embedded MyceliaDB, Sidecar-Vectorstore, Dry-Run, Auth, CSRF und Audit-Log.":"Local enterprise documentation with LM Studio, Embedded MyceliaDB, Sidecar vector store, dry-run, authentication, CSRF and audit log.",
    "Session wird geprüft ...":"Checking session ...",
    "Logout":"Logout",
    "Login / Registrierung":"Login / Registration",
    "Beim ersten Benutzer wird automatisch die Rolle admin vergeben. Danach einloggen und Jobs starten.":"The first registered user automatically receives the admin role. Then log in and start jobs.",
    "Benutzername":"Username",
    "Passwort":"Password",
    "Login":"Login",
    "Registrieren":"Register",
    "Generierung starten":"Start generation",
    "Projektdatei hochladen (.zip oder .md)":"Upload project file (.zip or .md)",
    "Oder lokaler Pfad auf dem Server":"Or local path on the server",
    "Ausführungsmodus":"Execution mode",
    "Mit LM Studio + Embedded MyceliaDB":"With LM Studio + Embedded MyceliaDB",
    "Mit LM Studio + Sidecar-Vectorstore":"With LM Studio + sidecar vector store",
    "Mit externer MyceliaDB-URL":"With external MyceliaDB URL",
    "Dry-Run ohne LLM":"Dry-run without LLM",
    "Dokumentationsprofil":"Documentation profile",
    "Quick — wenige LLM-Calls, Single-Pass-Finale":"Quick — fewer LLM calls, single-pass final document",
    "Balanced — reduzierte Kapitel, Module bleiben erhalten":"Balanced — reduced chapters, module reduction enabled",
    "Enterprise — vollständige Kapitelpipeline":"Enterprise — full chapter pipeline",
    "Quick ist ideal für Samples. Enterprise rendert jedes Kapitel separat und erzeugt deutlich mehr LLM-Aufrufe.":"Quick is ideal for samples. Enterprise renders each chapter separately and creates significantly more LLM calls.",
    "Kapitel optional, kommagetrennt":"Optional chapters, comma-separated",
    "Max. finale Kapitel":"Max final chapters",
    "Retrieval Top-K":"Retrieval Top-K",
    "Finale Doku als Single-Pass rendern":"Render final documentation as single-pass",
    "LLM-Modul-Reduktion überspringen":"Skip LLM module reduction",
    "Nur LLM-/Embedding-Aufwand schätzen":"Estimate LLM/embedding work only",
    "Projektname optional":"Optional project name",
    "Security / Mycelia":"Security / Mycelia",
    "Mycelia Local Token":"Mycelia local token",
    "Token einfügen oder generieren":"Paste or generate token",
    "Token generieren":"Generate token",
    "Temporär pro WebGUI-Prozess. Dauerhaft:":"Temporary for this WebGUI process. Persistent:",
    "LM Studio Chat-Modell":"LM Studio chat model",
    "LM Studio Embedding-Modell":"LM Studio embedding model",
    "LM Studio URL":"LM Studio URL",
    "MyceliaDB URL":"MyceliaDB URL",
    "Embedded Mycelia Port":"Embedded Mycelia port",
    "Max Workers":"Max workers",
    "Timeouts & Skalierung":"Timeouts & scaling",
    "Analysis Workers":"Analysis workers",
    "Chat Timeout":"Chat timeout",
    "Embedding Timeout":"Embedding timeout",
    "Gateway Timeout":"Gateway timeout",
    "Final Timeout":"Final timeout",
    "Shard-Größe":"Shard size",
    "Embedding Batch":"Embedding batch",
    "Analyse Tokens":"Analysis tokens",
    "Kapitel Tokens":"Chapter tokens",
    "LLM Retries":"LLM retries",
    "Retry Backoff":"Retry backoff",
    "Force Rebuild":"Force rebuild",
    "Adaptive Shard-Rettung deaktivieren":"Disable adaptive shard rescue",
    "Bei Timeout abbrechen statt Fallback schreiben":"Abort on timeout instead of writing fallback",
    "Integritätsfehler nur als Fallback erlauben (nicht empfohlen)":"Allow integrity errors only as fallback (not recommended)",
    "Standard: File-Reduce bricht ab, wenn keine Shard-Analysen gefunden werden. So werden leere Prompts wie „Keine Shard-Analysen verfügbar“ verhindert.":"Default: file-reduce aborts if no shard analyses are found. This prevents empty prompts such as “No shard analyses available”.",
    "Dokumentation erstellen":"Create documentation",
    "Status":"Status",
    "Noch kein Job gestartet.":"No job started yet.",
    "Logs":"Logs",
    "Dokumentation":"Documentation",
    "Jobs":"Jobs",
    "Metadaten":"Metadata",
    "Nicht angemeldet":"Not logged in",
    "Angemeldet als":"Logged in as",
    "Sessionprüfung fehlgeschlagen":"Session check failed",
    "Job wird gestartet ...":"Job is starting ...",
    "Start fehlgeschlagen":"Start failed",
    "unbekannter Fehler":"unknown error",
    "Fehler":"Error",
    "Dauer":"Duration",
    "HTML öffnen":"Open HTML",
    "Konnte Jobstatus nicht laden":"Could not load job status",
    "Polling-Fehler":"Polling error"
  },
  de: {}
};
let currentLang = localStorage.getItem("dfe_lang") || "de";
function translateNodeText(node, lang){
  if(lang === "de") return;
  const map = I18N[lang] || {};
  for(const child of node.childNodes){
    if(child.nodeType === Node.TEXT_NODE){
      const raw = child.nodeValue;
      const trimmed = raw.trim();
      if(map[trimmed]){
        child.nodeValue = raw.replace(trimmed, map[trimmed]);
      }
    } else if(child.nodeType === Node.ELEMENT_NODE && !["SCRIPT","STYLE","CODE","PRE","TEXTAREA","INPUT"].includes(child.tagName)){
      translateNodeText(child, lang);
    }
  }
}
function setPlaceholders(lang){
  if(lang === "en"){
    const ph = {
      "loginUser":"admin",
      "loginPass":"Secure password",
      "tok":"Paste or generate token"
    };
    for(const [id,value] of Object.entries(ph)){ const el=$(id); if(el) el.placeholder=value; }
    const pf=document.querySelector('[name="project_name"]'); if(pf) pf.placeholder="My Enterprise Project";
  }
}
function setLanguage(lang, userTriggered=false){
  lang = lang === "en" ? "en" : "de";
  localStorage.setItem("dfe_lang", lang);
  currentLang = lang;
  const lf = $("languageField"); if(lf) lf.value = lang;
  document.documentElement.lang = lang;
  if(lang === "en"){
    translateNodeText(document.body, "en");
    setPlaceholders("en");
    return;
  }
  if(userTriggered){
    location.reload();
  }
}
function tr(text){
  return currentLang === "en" ? ((I18N.en || {})[text] || text) : text;
}

let csrf = "";
let job = null;
let activeTab = "log";
let pollTimer = null;

function $(id){ return document.getElementById(id); }
async function api(u,o={}){ o.credentials='same-origin'; return fetch(u,o); }
function stopPolling(){
  if(pollTimer){
    clearTimeout(pollTimer);
    pollTimer=null;
  }
}
function showTab(name){
  activeTab=name;
  for(const tab of ['log','doc','jobs','meta']){
    const el=$('tab-'+tab);
    if(el) el.style.display = tab===name ? 'block' : 'none';
  }
}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function setLog(lines){
  const el=$('log');
  if(!el) return;
  const text=(lines||[]).join('\n');
  if(el.textContent !== text){
    el.textContent=text;
    el.scrollTop=el.scrollHeight;
  }
}

async function me(){
  try{
    stopPolling();
    let r=await api('/api/me');
    if(!r.ok) throw new Error('HTTP '+r.status);
    let d=await r.json();
    if(d.authenticated){
      csrf=d.csrf || "";
      const csrfEl=$('csrf'); if(csrfEl) csrfEl.value=csrf;
      $('authInfo').innerHTML=`<span class="pill">${tr("Angemeldet als")} <b>${esc(d.username)}</b> · ${esc(d.role)}</span>`;
      $('logoutBtn').style.display='inline-block';
      $('loginBox').style.display='none';
      $('app').style.display='grid';
      poll(true);
      return true;
    }else{
      csrf="";
      $('authInfo').innerHTML='<span class="pill">'+tr('Nicht angemeldet')+'</span>';
      $('logoutBtn').style.display='none';
      $('loginBox').style.display='block';
      $('app').style.display='none';
      setLog([]);
      return false;
    }
  }catch(err){
    csrf="";
    stopPolling();
    $('authInfo').innerHTML='<span class="pill">'+tr('Sessionprüfung fehlgeschlagen')+'</span>';
    $('loginBox').style.display='block';
    $('app').style.display='none';
    const msg=$('authMsg'); if(msg) msg.textContent='Session check failed: '+err;
    return false;
  }
}

async function login(){
  let b=new URLSearchParams({username:$('loginUser').value,password:$('loginPass').value});
  let r=await api('/api/login',{method:'POST',body:b});
  $('authMsg').textContent=JSON.stringify(await r.json(),null,2);
  await me();
}
async function register(){
  let b=new URLSearchParams({username:$('loginUser').value,password:$('loginPass').value});
  let r=await api('/api/register',{method:'POST',body:b});
  $('authMsg').textContent=JSON.stringify(await r.json(),null,2);
  await me();
}
async function logout(){
  stopPolling();
  await api('/api/logout',{method:'POST',headers:{'X-CSRF-Token':csrf}});
  job=null;
  await me();
}
async function gen(){
  let r=await api('/api/token',{method:'POST',headers:{'X-CSRF-Token':csrf}});
  $('tok').value=(await r.json()).token;
}

$('f').onsubmit=async e=>{
  e.preventDefault();
  setLog(["[WebGUI] "+(currentLang==="en"?"Starting job ...":"Starte Job ...")]);
  $('current').innerHTML='<span class="status running">starting</span> '+tr('Job wird gestartet ...');
  let fd=new FormData($('f'));
  let r=await api('/api/start',{method:'POST',headers:{'X-CSRF-Token':csrf},body:fd});
  let d=await r.json();
  if(!r.ok){
    setLog(["[WebGUI] Start fehlgeschlagen: "+(d.error||'unbekannter Fehler')]);
    alert(d.error||tr('Start fehlgeschlagen'));
    return;
  }
  job=d.job_id;
  showTab('log');
  await poll(true);
};

async function poll(immediate=false){
  if(!csrf){
    stopPolling();
    return;
  }
  try{
    if(job){
      let response=await api('/api/job/'+job);
      if(response.status===401 || response.status===403){ stopPolling(); await me(); return; }
      if(response.ok){
        let d=await response.json();
        $('current').innerHTML=`<span class="status ${d.status}">${d.status}</span> Job ${d.id||''} · ${tr("Dauer")} ${d.duration_seconds||0}s`+(d.error?`<br><b>${tr("Fehler")}:</b> ${esc(d.error)}`:'');
        setLog(d.log||[]);
        const jobsResponse=await api('/api/jobs');
        if(jobsResponse.ok) $('jobs').textContent=JSON.stringify(await jobsResponse.json(),null,2);
        $('meta').textContent=JSON.stringify(d,null,2);
        if(d.status==='success'&&d.output_html){
          $('doc').src='/api/job/'+d.id+'/html';
          $('docLinks').innerHTML=`<a href="/api/job/${d.id}/html" target="_blank">${tr("HTML öffnen")}</a>`;
        }
      }else{
        setLog(["[WebGUI] "+tr("Konnte Jobstatus nicht laden")+": HTTP "+response.status]);
      }
    }else{
      let r=await api('/api/jobs');
      if(r.status===401 || r.status===403){ stopPolling(); await me(); return; }
      if(r.ok) $('jobs').textContent=JSON.stringify(await r.json(),null,2);
    }
  }catch(err){
    const old=$('log') ? $('log').textContent : '';
    setLog((old?old.split('\n'):[]).concat(["[WebGUI] "+tr("Polling-Fehler")+": "+err]));
  }finally{
    if(csrf){
      if(pollTimer) clearTimeout(pollTimer);
      pollTimer=setTimeout(()=>poll(false), immediate?500:1500);
    }
  }
}
const langSel=$("uiLang"); if(langSel){ langSel.value=currentLang; }
setTimeout(()=>setLanguage(currentLang,false),0);
setTimeout(()=>me(),50);
</script>
</body>
</html>"""
class Handler(BaseHTTPRequestHandler):
    reg:Registry; read_only=False; max_upload=100_000_000
    def _safe_write(self, raw: bytes) -> bool:
        try:
            self.wfile.write(raw)
            return True
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            # Browsers may cancel polling/session requests during reloads or language switches.
            # This is not a server-side failure and must never spam the console or break UI state.
            return False

    def _json(self,b,status=200,headers=None):
        raw=json.dumps(b,ensure_ascii=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(raw)))
            self.send_header("Cache-Control","no-store")
            for k,v in (headers or {}).items(): self.send_header(k,v)
            self.end_headers()
            self._safe_write(raw)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return None

    def _text(self,t,ct="text/html",status=200):
        raw=t.encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type",ct+"; charset=utf-8")
            self.send_header("Content-Length",str(len(raw)))
            self.send_header("Cache-Control","no-store")
            self.end_headers()
            self._safe_write(raw)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return None
    def _sess(self):
        try:
            tok = _parse_cookies(self.headers.get("Cookie")).get("dfe_session", "")
            if not isinstance(tok, str) or not tok.strip():
                return None, ""
            return self.reg.auth.session(tok), tok
        except Exception:
            # Invalid/corrupted cookies must be treated as anonymous, never as server errors.
            return None, ""
    def _csrf_ok(self,s):
        return bool(s and self.headers.get("X-CSRF-Token")==s["csrf"])
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/": return self._text(HTML)
        if path=="/api/me":
            s,t=self._sess(); return self._json({"authenticated":bool(s), **(s or {})})
        if not self._sess()[0]: return self._json({"error":"auth required"},401)
        if path=="/api/jobs": return self._json(self.reg.list())
        if path.startswith("/api/job/"):
            parts=path.strip("/").split("/"); j=self.reg.get(parts[2]) if len(parts)>=3 else None
            if not j: return self._json({"error":"not found"},404)
            if len(parts)==3: return self._json(j.public())
            if parts[3]=="html" and j.output_html and Path(j.output_html).exists(): return self._text(Path(j.output_html).read_text(encoding="utf-8"))
        return self._json({"error":"not found"},404)
    def do_POST(self):
        path=urlparse(self.path).path
        if path in {"/api/login","/api/register"}:
            body=self.rfile.read(int(self.headers.get("Content-Length","0"))); f=_form_urlencoded(body); u=f.get("username","").strip(); pw=f.get("password","")
            if path.endswith("register"):
                if self.reg.auth.user_count()>0 and not True: return self._json({"error":"registration disabled"},403)
                try: self.reg.auth.register(u,pw)
                except Exception as e: return self._json({"error":str(e)},400)
            if not self.reg.auth.verify(u,pw): return self._json({"error":"invalid login"},403)
            tok,csrf=self.reg.auth.create_session(u); return self._json({"ok":True,"csrf":csrf},headers={"Set-Cookie":f"dfe_session={tok}; HttpOnly; SameSite=Strict; Path=/"})
        s,t=self._sess()
        if not s: return self._json({"error":"auth required"},401)
        if not self._csrf_ok(s): return self._json({"error":"csrf failed"},403)
        if path=="/api/logout": self.reg.auth.logout(t); return self._json({"ok":True},headers={"Set-Cookie":"dfe_session=; Max-Age=0; Path=/"})
        if path=="/api/token": return self._json({"token":secrets.token_urlsafe(32)})
        if path=="/api/start":
            if self.read_only or s["role"] not in {"admin","operator"}: return self._json({"error":"read only or insufficient role"},403)
            length=int(self.headers.get("Content-Length","0"))
            if length>self.max_upload+200_000: return self._json({"error":"request too large"},413)
            body=self.rfile.read(length); fields,files=_parse_multipart(body,self.headers.get("Content-Type",""),self.max_upload)
            job=self.reg.create(); jd=self.reg.root/"jobs"/job.id; inp=jd/"input"; ws=jd/"workspace"; inp.mkdir(parents=True,exist_ok=True)
            input_path=Path(fields.get("input_path","")) if fields.get("input_path") else None
            if "upload" in files: fn,data=files["upload"]; input_path=inp/fn; input_path.write_bytes(data)
            if not input_path or not input_path.exists(): return self._json({"error":"input missing"},400)
            self.reg.log(job, f"[WebGUI] Job {job.id} wurde angenommen und in die Ausführung gegeben.")
            self.reg.log(job, f"[WebGUI] Datei/Pfad: {input_path}")
            threading.Thread(target=run_job,args=(self.reg,job,fields,input_path,ws,s["username"]),daemon=True).start()
            return self._json({"job_id":job.id})
        return self._json({"error":"not found"},404)
    def log_message(self,*a): return
def main(argv=None):
    import argparse
    p=argparse.ArgumentParser(); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=7860); p.add_argument("--root",type=Path,default=Path(".docforge_webgui")); p.add_argument("--read-only",action="store_true"); p.add_argument("--max-upload-mb",type=int,default=100)
    a=p.parse_args(argv); Handler.reg=Registry(a.root); Handler.read_only=a.read_only; Handler.max_upload=a.max_upload_mb*1024*1024
    srv=ThreadingHTTPServer((a.host,a.port),Handler); print(f"DocForge Secure WebGUI: http://{a.host}:{a.port}"); srv.serve_forever()
if __name__=="__main__": raise SystemExit(main())
