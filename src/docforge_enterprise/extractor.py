from __future__ import annotations
import zipfile, shutil
from pathlib import Path
from typing import Iterable
from .config import Settings
from .hashing import sha256_text
from .models import ProjectFile
from .security import should_skip_path, is_probably_binary, redact_secrets
EXT={".py":("python","code"),".js":("javascript","code"),".ts":("typescript","code"),".java":("java","code"),".cs":("csharp","code"),".go":("go","code"),".rs":("rust","code"),".md":("markdown","documentation"),".txt":("text","documentation"),".json":("json","config"),".yaml":("yaml","config"),".yml":("yaml","config"),".toml":("toml","config"),".sql":("sql","data")}
def _safe_extract(zip_path:Path,target:Path,fail:bool=True)->None:
    target=target.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            out=(target/info.filename).resolve()
            if not str(out).startswith(str(target)):
                if fail: raise ValueError(f"Unsafe ZIP path: {info.filename}")
                continue
            z.extract(info,target)
def prepare_input(input_path:Path, workspace:Path, settings:Settings)->Path:
    extracted=workspace/"extracted"; extracted.mkdir(parents=True,exist_ok=True)
    if input_path.is_dir(): return input_path
    if input_path.suffix.lower()==".zip": _safe_extract(input_path,extracted,settings.security.fail_on_zip_slip); return extracted
    if input_path.suffix.lower() in {".md",".txt"}:
        (extracted/input_path.name).write_bytes(input_path.read_bytes()); return extracted
    raise ValueError("Input must be .zip, .md, .txt or directory")
def detect(path:Path):
    return EXT.get(path.suffix.lower(),("unknown","unknown"))
def iter_project_files(root:Path, settings:Settings)->Iterable[ProjectFile]:
    for path in root.rglob("*"):
        if not path.is_file(): continue
        rel=str(path.relative_to(root)).replace("\\","/")
        if should_skip_path(Path(rel),settings.security).allowed: continue
        size=path.stat().st_size
        if size>settings.security.max_file_bytes: continue
        raw=path.read_bytes()
        if not settings.security.allow_binary and is_probably_binary(raw): continue
        lang,kind=detect(path)
        if lang=="unknown": continue
        try: text=raw.decode("utf-8-sig")
        except UnicodeDecodeError: text=raw.decode("latin-1","replace")
        if settings.security.redact_secrets: text,_=redact_secrets(text)
        yield ProjectFile(path,rel,lang,kind,text,sha256_text(text),size)
