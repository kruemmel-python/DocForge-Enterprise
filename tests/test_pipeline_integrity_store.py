from pathlib import Path

from docforge_enterprise.models import AnalysisRecord, CodeShard, ProjectFile
from docforge_enterprise.store import AnalysisStore


def test_store_maps_shard_analyses_to_file(tmp_path: Path) -> None:
    store = AnalysisStore(tmp_path / "analysis.sqlite3")
    try:
        project_file = ProjectFile(
            path=tmp_path / "src/app.py",
            relative_path="src/app.py",
            language="python",
            kind="code",
            content="def main():\n    return 1\n",
            sha256="filehash",
            size_bytes=24,
        )
        shard = CodeShard(
            id="shard-1",
            file_path="src/app.py",
            language="python",
            kind="code",
            content="def main():\n    return 1\n",
            char_start=0,
            char_end=24,
            sha256="shardhash",
            ordinal=0,
            symbols=("main",),
        )

        store.upsert_files([project_file])
        store.upsert_shards([shard])
        store.save_analysis(
            AnalysisRecord(
                id="analysis-1",
                stage="shard",
                source_id="shard-1",
                payload={"file_path": "src/app.py", "shard_id": "shard-1", "purpose": "test"},
            )
        )

        assert store.shard_ids_for_file("src/app.py") == ["shard-1"]
        assert store.get_analysis("shard", "shard-1")["file_path"] == "src/app.py"
        assert store.analysis_count_for_file("src/app.py") == 1
    finally:
        store.close()
