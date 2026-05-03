from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
class Handler(BaseHTTPRequestHandler):
    token=""; quiet=True
    def _json(self,b,status=200):
        raw=json.dumps(b).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if self.path in {"/","/health","/status"}: self._json({"status":"ok","service":"embedded-myceliadb","version":"5.0.1"}); return
        self._json({"status":"error"},404)
    def do_POST(self):
        if self.token and self.headers.get("X-Mycelia-Local-Token","")!=self.token: self._json({"status":"error","message":"token mismatch"},403); return
        self._json({"status":"ok","mode":"compat","message":"embedded gateway active"})
    def log_message(self,*a): 
        if not self.quiet: super().log_message(*a)
def start_server(*,host="127.0.0.1",port=9999,root=Path(".docforge_workspace/embedded_myceliadb"),token="",quiet=True):
    cls=type("EmbeddedHandler",(Handler,),{"token":token,"quiet":quiet})
    return ThreadingHTTPServer((host,port),cls)
def serve(host="127.0.0.1",port=9999,root=Path(".docforge_workspace/embedded_myceliadb"),token="",quiet=False):
    s=start_server(host=host,port=port,root=root,token=token,quiet=quiet); print(f"embedded-myceliadb on http://{host}:{port}"); s.serve_forever()
