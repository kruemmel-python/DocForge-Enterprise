from __future__ import annotations
import ast, re
from dataclasses import dataclass
from typing import Iterable
from .hashing import stable_id, sha256_text
from .models import ProjectFile, CodeShard
@dataclass(slots=True)
class ShardPlan: max_chars:int=2500; overlap:int=300
SYM=re.compile(r"(?m)^\s*(?:class|def|async\s+def|function|interface|type|public\s+class)\s+([A-Za-z_][\w]*)")
def _line_offsets(t): 
    o=[0]; s=0
    for line in t.splitlines(True): s+=len(line); o.append(s)
    return o
def _py_spans(text):
    try: tree=ast.parse(text)
    except SyntaxError: return []
    offs=_line_offsets(text); spans=[]
    for n in ast.walk(tree):
        if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and hasattr(n,"end_lineno"):
            spans.append((offs[n.lineno-1],offs[n.end_lineno],(n.name,)))
    return sorted(spans)
def _md_spans(text):
    hs=list(re.finditer(r"(?m)^#{1,6}\s+(.+)$",text)); 
    return [(h.start(), hs[i+1].start() if i+1<len(hs) else len(text),(h.group(1).strip(),)) for i,h in enumerate(hs)]
def _split(f,start,end,ord0,plan,symbols=()):
    out=[]; pos=start; ordinal=ord0
    while pos<end:
        ce=min(pos+plan.max_chars,end)
        if ce<end:
            nl=f.content.rfind("\n",pos,ce)
            if nl>pos+plan.max_chars//2: ce=nl
        text=f.content[pos:ce]
        if text.strip():
            out.append(CodeShard(stable_id(f.relative_path,f.sha256,pos,ce),f.relative_path,f.language,f.kind,text,pos,ce,sha256_text(text),ordinal,symbols))
            ordinal+=1
        pos=ce if ce>=end else max(ce-plan.overlap,pos+1)
    return out
def shard_file(f:ProjectFile, plan:ShardPlan)->list[CodeShard]:
    spans=_py_spans(f.content) if f.language=="python" else (_md_spans(f.content) if f.language=="markdown" else [])
    if spans:
        out=[]; ordn=0; covered=0
        for s,e,sym in spans:
            if s>covered: out+=_split(f,covered,s,ordn,plan); ordn=len(out)
            out+=_split(f,s,e,ordn,plan,sym); ordn=len(out); covered=max(covered,e)
        if covered<len(f.content): out+=_split(f,covered,len(f.content),ordn,plan)
        return out
    syms=tuple(dict.fromkeys(SYM.findall(f.content)))
    return _split(f,0,len(f.content),0,plan,syms)
def shard_project(files:Iterable[ProjectFile], plan:ShardPlan)->list[CodeShard]:
    out=[]
    for f in files: out+=shard_file(f,plan)
    return out
