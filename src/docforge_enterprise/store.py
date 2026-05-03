from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .hashing import stable_json
from .models import AnalysisRecord, CodeShard, ProjectFile


class AnalysisStore:
    """Thread-safe SQLite control store for DocForge pipeline state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                create table if not exists project_files (
                    relative_path text primary key,
                    language text not null,
                    kind text not null,
                    sha256 text not null,
                    size_bytes integer not null,
                    content text not null,
                    created_at real not null
                );

                create table if not exists shards (
                    id text primary key,
                    file_path text not null,
                    language text not null,
                    kind text not null,
                    sha256 text not null,
                    ordinal integer not null,
                    char_start integer not null,
                    char_end integer not null,
                    symbols_json text not null,
                    content text not null,
                    created_at real not null
                );

                create table if not exists analysis_records (
                    id text primary key,
                    stage text not null,
                    source_id text not null,
                    status text not null,
                    error text not null,
                    payload_json text not null,
                    created_at real not null
                );

                create index if not exists idx_analysis_stage_source
                on analysis_records(stage, source_id);

                create table if not exists retrieval_events (
                    id integer primary key autoincrement,
                    query text not null,
                    target_id text not null,
                    metadata_json text not null,
                    created_at real not null
                );

                create table if not exists checkpoints (
                    stage text primary key,
                    payload_json text not null,
                    updated_at real not null
                );
                """
            )
            self.conn.commit()

    def upsert_files(self, files: Iterable[ProjectFile]) -> None:
        now = time.time()
        rows = [
            (f.relative_path, f.language, f.kind, f.sha256, f.size_bytes, f.content, now)
            for f in files
        ]
        with self._lock:
            self.conn.executemany(
                """
                insert into project_files(relative_path, language, kind, sha256, size_bytes, content, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(relative_path) do update set
                    language=excluded.language,
                    kind=excluded.kind,
                    sha256=excluded.sha256,
                    size_bytes=excluded.size_bytes,
                    content=excluded.content,
                    created_at=excluded.created_at
                """,
                rows,
            )
            self.conn.commit()

    def upsert_shards(self, shards: Iterable[CodeShard]) -> None:
        now = time.time()
        rows = [
            (
                s.id,
                s.file_path,
                s.language,
                s.kind,
                s.sha256,
                s.ordinal,
                s.char_start,
                s.char_end,
                json.dumps(list(s.symbols), ensure_ascii=False),
                s.content,
                now,
            )
            for s in shards
        ]
        with self._lock:
            self.conn.executemany(
                """
                insert into shards(id, file_path, language, kind, sha256, ordinal, char_start, char_end, symbols_json, content, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    file_path=excluded.file_path,
                    language=excluded.language,
                    kind=excluded.kind,
                    sha256=excluded.sha256,
                    ordinal=excluded.ordinal,
                    char_start=excluded.char_start,
                    char_end=excluded.char_end,
                    symbols_json=excluded.symbols_json,
                    content=excluded.content,
                    created_at=excluded.created_at
                """,
                rows,
            )
            self.conn.commit()

    def save_analysis(self, record: AnalysisRecord) -> None:
        with self._lock:
            self.conn.execute(
                """
                insert into analysis_records(id, stage, source_id, status, error, payload_json, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    stage=excluded.stage,
                    source_id=excluded.source_id,
                    status=excluded.status,
                    error=excluded.error,
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (
                    record.id,
                    record.stage,
                    record.source_id,
                    record.status,
                    record.error,
                    stable_json(record.payload),
                    time.time(),
                ),
            )
            self.conn.commit()

    def get_analysis(self, stage: str, source_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                """
                select payload_json from analysis_records
                where stage = ? and source_id = ? and status = 'ok'
                order by created_at desc limit 1
                """,
                (stage, source_id),
            ).fetchone()
        if row is None:
            return None
        return json.loads(str(row["payload_json"]))

    def list_analysis(self, stage: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "select payload_json from analysis_records where stage = ? and status = 'ok'",
                (stage,),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def shard_ids_for_file(self, file_path: str) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "select id from shards where file_path = ? order by ordinal",
                (file_path,),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def save_retrieval_event(self, *, query: str, target_id: str, metadata: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """
                insert into retrieval_events(query, target_id, metadata_json, created_at)
                values (?, ?, ?, ?)
                """,
                (query, target_id, stable_json(metadata), time.time()),
            )
            self.conn.commit()


    def save_checkpoint(self, stage: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """
                insert into checkpoints(stage, payload_json, updated_at)
                values (?, ?, ?)
                on conflict(stage) do update set
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (stage, stable_json(payload), time.time()),
            )
            self.conn.commit()

    def get_checkpoint(self, stage: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "select payload_json from checkpoints where stage = ?",
                (stage,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(str(row["payload_json"]))
