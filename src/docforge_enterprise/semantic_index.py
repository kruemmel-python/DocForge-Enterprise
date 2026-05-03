from __future__ import annotations
import json, math, struct, hashlib, urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from .config import Settings
from .models import CodeShard, RetrievedContext
def _hash_embed(text:str,dim:int=128)->list[float]:
    vec=[0.0]*dim
    for tok in text.lower().split():
        h=int(hashlib.sha256(tok.encode()).hexdigest(),16); vec[h%dim]+=1.0
    n=math.sqrt(sum(x*x for x in vec)) or 1.0
    return [x/n for x in vec]
def _cos(a,b): return sum(x*y for x,y in zip(a,b))
@dataclass(slots=True)
class SemanticIndex:
    settings:Settings; collection:str; records:list[dict[str,Any]]=field(default_factory=list)
    def embedding_text(self, s:CodeShard)->str:
        return f"{s.file_path} {s.language} {' '.join(s.symbols)}\n{s.content}"
    def ingest(self, shards:Iterable[CodeShard], *, batch_size:int=8)->list[dict[str,Any]]:
        self.records=[]
        for s in shards:
            t=self.embedding_text(s); self.records.append({"id":s.id,"vector":_hash_embed(t),"text":t,"file_path":s.file_path,"metadata":{"file_path":s.file_path,"language":s.language,"symbols":list(s.symbols)}})
        return [{"status":"ok","collection":self.collection,"count":len(self.records),"merkle_head":hashlib.sha256(str(len(self.records)).encode()).hexdigest()}]
    def query(self, query:str, *, limit:int|None=None)->tuple[list[RetrievedContext],dict[str,Any]]:
        q=_hash_embed(query); limit=limit or self.settings.pipeline.retrieval_limit
        scored=sorted((( _cos(q,r["vector"]), r) for r in self.records), reverse=True, key=lambda x:x[0])[:limit]
        return [RetrievedContext(r["id"],float(score),r["file_path"],r["text"],r["metadata"]) for score,r in scored], {"backend":"embedded-sidecar","count":len(scored)}
