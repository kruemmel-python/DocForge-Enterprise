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
def build_command(payload,input_path:Path,workspace:Path):
    cmd=[sys.executable,"-m","docforge_enterprise.cli",str(input_path),"--workspace",str(workspace),"--profile",payload.get("profile","balanced"),"--chat-model",payload.get("chat_model") or DEFAULT_CHAT_MODEL,"--embedding-model",payload.get("embedding_model") or DEFAULT_EMBEDDING_MODEL,"--analysis-workers",payload.get("analysis_workers","1"),"--chat-timeout",payload.get("chat_timeout","600"),"--embedding-timeout",payload.get("embedding_timeout","300"),"--gateway-timeout",payload.get("gateway_timeout","180"),"--max-chars-per-shard",payload.get("max_chars_per_shard","2500"),"--max-embedding-batch-size",payload.get("max_embedding_batch_size","4"),"--analysis-max-tokens",payload.get("analysis_max_tokens","900"),"--llm-retries",payload.get("llm_retries","3")]
    if payload.get("chapters"): cmd+=["--chapters",payload["chapters"]]
    for flag in ["single_pass_final","disable_module_reduce","estimate_only","force_rebuild"]:
        if _bool(payload.get(flag)): cmd.append("--"+flag.replace("_","-"))
    mode=payload.get("mode","embedded_mycelia")
    if mode=="dry_run": cmd.append("--dry-run")
    elif mode=="sidecar_lmstudio": cmd.append("--sidecar-only")
    elif mode=="embedded_mycelia": cmd.append("--embedded-mycelia")
    return cmd
def run_job(reg,j,payload,input_path,workspace,actor):
    with reg.lock: j.status="running"; j.started_at=time.time(); j.input_path=str(input_path); j.workspace=str(workspace); j.command=build_command(payload,input_path,workspace)
    reg.auth.audit(actor,"job_start",j.id,metadata=json.dumps({"cmd":j.command}))
    env=os.environ.copy()
    if payload.get("mycelia_token"): env["MYCELIA_LOCAL_TOKEN"]=payload["mycelia_token"]
    try:
        p=subprocess.Popen(j.command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",errors="replace",env=env)
        assert p.stdout
        for line in p.stdout: reg.log(j,line)
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
HTML="""<!doctype html><html><head><meta charset=utf-8><title>DocForge v5.0.1 Secure WebGUI</title><style>body{font-family:system-ui;margin:2rem;background:#0f172a;color:#e5e7eb}input,select,textarea,button{width:100%;padding:.6rem;margin:.25rem 0;border-radius:8px}section{background:#111827;padding:1rem;border-radius:14px;margin:1rem 0}pre{white-space:pre-wrap;background:#020617;padding:1rem;border-radius:10px}.ok{color:#22c55e}.bad{color:#ef4444}</style></head><body><h1>DocForge Enterprise v5.0.1 Secure WebGUI</h1><div id=auth></div><section id=app style='display:none'><h2>Job starten</h2><form id=f enctype=multipart/form-data><input type=hidden name=csrf id=csrf><label>Upload .zip/.md</label><input type=file name=upload><label>oder lokaler Pfad</label><input name=input_path><label>Modus</label><select name=mode><option value=embedded_mycelia>LM Studio + Embedded MyceliaDB</option><option value=sidecar_lmstudio>LM Studio + Sidecar</option><option value=dry_run>Dry-Run</option></select><label>Profil</label><select name=profile><option>quick</option><option selected>balanced</option><option>enterprise</option></select><label>Kapitel</label><textarea name=chapters></textarea><label>Chat Model</label><input name=chat_model value='google_gemma-4-e4b-it'><label>Embedding Model</label><input name=embedding_model value='text-embedding-nomic-embed-text-v2-moe'><label>Token</label><input name=mycelia_token id=tok><button type=button onclick=gen()>Token generieren</button><label><input type=checkbox name=force_rebuild checked style='width:auto'> Force Rebuild</label><label><input type=checkbox name=estimate_only style='width:auto'> Nur schätzen</label><button>Start</button></form></section><section><h2>Status</h2><pre id=status></pre><pre id=log></pre><iframe id=doc style='width:100%;height:600px;background:white'></iframe></section><script>
let csrf="",job=null;async function api(u,o={}){o.credentials='same-origin';return fetch(u,o)}async function me(){let r=await api('/api/me');let d=await r.json();if(d.authenticated){csrf=d.csrf;document.getElementById('csrf').value=csrf;auth.innerHTML='Angemeldet als '+d.username+' ('+d.role+') <button onclick=logout()>Logout</button>';app.style.display='block'}else{auth.innerHTML='<section><h2>Login / Registrierung</h2><input id=u placeholder=Benutzer><input id=p type=password placeholder=Passwort><button onclick=login()>Login</button><button onclick=register()>Registrieren</button></section>';app.style.display='none'}}async function login(){let b=new URLSearchParams({username:u.value,password:p.value});await api('/api/login',{method:'POST',body:b});me()}async function register(){let b=new URLSearchParams({username:u.value,password:p.value});await api('/api/register',{method:'POST',body:b});me()}async function logout(){await api('/api/logout',{method:'POST',headers:{'X-CSRF-Token':csrf}});me()}async function gen(){let r=await api('/api/token',{method:'POST',headers:{'X-CSRF-Token':csrf}});tok.value=(await r.json()).token}f.onsubmit=async e=>{e.preventDefault();let fd=new FormData(f);let r=await api('/api/start',{method:'POST',headers:{'X-CSRF-Token':csrf},body:fd});let d=await r.json();job=d.job_id;poll()};async function poll(){if(job){let d=await (await api('/api/job/'+job)).json();status.textContent=JSON.stringify(d,null,2);log.textContent=(d.log||[]).join('\\n');if(d.status==='success'&&d.output_html)doc.src='/api/job/'+job+'/html'}setTimeout(poll,1500)}me();poll();</script></body></html>"""
class Handler(BaseHTTPRequestHandler):
    reg:Registry; read_only=False; max_upload=100_000_000
    def _json(self,b,status=200,headers=None):
        raw=json.dumps(b,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw))); 
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(raw)
    def _text(self,t,ct="text/html",status=200):
        raw=t.encode(); self.send_response(status); self.send_header("Content-Type",ct+"; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def _sess(self):
        tok=_parse_cookies(self.headers.get("Cookie")).get("dfe_session",""); return self.reg.auth.session(tok),tok
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
