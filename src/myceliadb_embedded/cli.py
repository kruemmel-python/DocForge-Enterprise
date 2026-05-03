from __future__ import annotations
import argparse, os
from pathlib import Path
from .gateway import serve
def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=9999); p.add_argument("--root",type=Path,default=Path(".docforge_workspace/embedded_myceliadb")); p.add_argument("--token",default=""); a=p.parse_args(argv); serve(host=a.host,port=a.port,root=a.root,token=a.token or os.getenv("MYCELIA_LOCAL_TOKEN","")); return 0
if __name__=="__main__": raise SystemExit(main())
