from __future__ import annotations
import json, sqlite3, threading, time
from pathlib import Path
from typing import Any, Iterable
from .hashing import stable_json
from .models import AnalysisRecord, ProjectFile, CodeShard
class AnalysisStore:
    def __init__(self,path:Path):
        self.path=path; path.parent.mkdir(parents=True,exist_ok=True); self.lock=threading.RLock(); self.conn=sqlite3.connect(path,check_same_thread=False); self.conn.row_factory=sqlite3.Row; self._init()
    def _init(self):
        with self.lock:
            self.conn.executescript("""create table if not exists analysis_records(id text primary key, stage text, source_id text, status text, error text, payload_json text, created_at real);
            create table if not exists retrieval_events(id integer primary key, query text, target_id text, metadata_json text, created_at real);
            create table if not exists checkpoints(stage text primary key, payload_json text, updated_at real);
            create table if not exists web_audit(id integer primary key, actor text, action text, target text, metadata_json text, created_at real);"""); self.conn.commit()
    def close(self): self.conn.close()
    def upsert_files(self,files:Iterable[ProjectFile]): pass
    def upsert_shards(self,shards:Iterable[CodeShard]): pass
    def save_analysis(self,r:AnalysisRecord):
        with self.lock: self.conn.execute("insert or replace into analysis_records values(?,?,?,?,?,?,?)",(r.id,r.stage,r.source_id,r.status,r.error,stable_json(r.payload),time.time())); self.conn.commit()
    def get_analysis(self,stage,source_id):
        with self.lock: row=self.conn.execute("select payload_json from analysis_records where stage=? and source_id=? and status='ok' order by created_at desc limit 1",(stage,source_id)).fetchone()
        return json.loads(row["payload_json"]) if row else None
    def list_analysis(self,stage):
        with self.lock: rows=self.conn.execute("select payload_json from analysis_records where stage=? and status='ok'",(stage,)).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]
    def shard_ids_for_file(self,file_path): 
        rows=self.conn.execute("select source_id from analysis_records where stage='shard'").fetchall(); return [r["source_id"] for r in rows]
    def save_retrieval_event(self,*,query,target_id,metadata):
        with self.lock: self.conn.execute("insert into retrieval_events(query,target_id,metadata_json,created_at) values(?,?,?,?)",(query,target_id,stable_json(metadata),time.time())); self.conn.commit()
    def save_checkpoint(self,stage,payload):
        with self.lock: self.conn.execute("insert or replace into checkpoints values(?,?,?)",(stage,stable_json(payload),time.time())); self.conn.commit()
    def audit(self,actor,action,target="",metadata=None):
        with self.lock: self.conn.execute("insert into web_audit(actor,action,target,metadata_json,created_at) values(?,?,?,?,?)",(actor,action,target,stable_json(metadata or {}),time.time())); self.conn.commit()
