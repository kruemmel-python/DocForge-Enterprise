from pathlib import Path

from docforge_enterprise.webgui import build_command


def test_webgui_builds_embedded_command(tmp_path: Path) -> None:
    cmd = build_command(
        {
            "mode": "embedded_mycelia",
            "chat_model": "google_gemma-4-e4b-it",
            "embedding_model": "text-embedding-nomic-embed-text-v2-moe",
            "analysis_workers": "1",
            "chat_timeout": "600",
            "embedding_timeout": "300",
            "gateway_timeout": "180",
            "max_chars_per_shard": "2500",
            "max_embedding_batch_size": "4",
            "analysis_max_tokens": "900",
            "llm_retries": "3",
            "force_rebuild": True,
        },
        tmp_path / "project.zip",
        tmp_path / "workspace",
    )
    assert "-m" in cmd
    assert "docforge_enterprise.cli" in cmd
    assert "--embedded-mycelia" in cmd
    assert "--chat-model" in cmd
    assert "google_gemma-4-e4b-it" in cmd
    assert "--embedding-model" in cmd
    assert "text-embedding-nomic-embed-text-v2-moe" in cmd
