from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class LMStudioSettings:
    base_url: str = "http://127.0.0.1:1234/v1"
    chat_model: str = "local-model"
    embedding_model: str = "text-embedding-nomic-embed-text-v1.5"

    # Backward-compatible global timeout. Specific budgets below override it.
    timeout_seconds: float = 180.0

    # v0.3 request budgets. Local LM Studio models can be slow, especially on CPU
    # or when multiple workers hit one model. Keep defaults conservative.
    chat_timeout_seconds: float = 300.0
    embedding_timeout_seconds: float = 180.0
    gateway_timeout_seconds: float = 120.0
    final_timeout_seconds: float = 600.0

    request_retries: int = 3
    retry_backoff_seconds: float = 2.0

    temperature: float = 0.1
    max_json_tokens: int = 1200
    max_chapter_tokens: int = 3500
    json_repair_attempts: int = 1


@dataclass(slots=True)
class MyceliaSettings:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:9999"
    token: str = ""
    token_env: str = "MYCELIA_LOCAL_TOKEN"
    vault_path: Path = Path(".docforge_workspace/mycelia_vault")
    collection_prefix: str = "docforge"
    search_backend: str = "auto"  # auto | sidecar | mycelia
    sealed_mode: str = "auto"     # off | auto | required
    store_text: bool = True
    default_dimension: int = 768


@dataclass(slots=True)
class PipelineSettings:
    workspace: Path = Path(".docforge_workspace")
    max_chars_per_shard: int = 3500
    min_chars_per_shard: int = 1200
    shard_overlap: int = 400
    retrieval_limit: int = 8
    batch_size: int = 16
    max_embedding_batch_size: int = 8
    analysis_workers: int = 1
    max_analysis_workers: int = 2
    dry_run: bool = False
    force_rebuild: bool = False
    emit_html: bool = True
    emit_markdown: bool = True
    emit_json: bool = True
    project_name: str = ""

    # v0.5 documentation profiles.
    # quick      = fewer LLM calls, one compact final pass, useful for small projects/tests.
    # balanced   = reduced chapter set, still uses file/module reduction.
    # enterprise = full chapter set and deepest synthesis.
    profile: str = "enterprise"
    chapters: str = ""
    single_pass_final: bool = False
    disable_module_reduce: bool = False
    max_final_chapters: int = 0
    estimate_only: bool = False
    explain_llm_calls: bool = True

    # v0.3 resilience controls.
    checkpoint_every: int = 1
    adaptive_shard_on_timeout: bool = True
    continue_on_timeout: bool = True
    chapter_context_file_limit: int = 80
    chapter_context_module_limit: int = 40


@dataclass(slots=True)
class SecuritySettings:
    max_file_bytes: int = 2_000_000
    block_secret_files: bool = True
    block_vendor_dirs: bool = True
    allow_binary: bool = False
    redact_secrets: bool = True
    fail_on_zip_slip: bool = True


@dataclass(slots=True)
class Settings:
    lmstudio: LMStudioSettings = field(default_factory=LMStudioSettings)
    mycelia: MyceliaSettings = field(default_factory=MyceliaSettings)
    pipeline: PipelineSettings = field(default_factory=PipelineSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)

    @classmethod
    def from_toml(cls, path: Path | None) -> "Settings":
        settings = cls()
        if path is None:
            settings._apply_env()
            return settings
        data = tomllib.loads(path.read_text(encoding="utf-8"))

        def section(name: str) -> dict:
            raw = data.get(name, {})
            return raw if isinstance(raw, dict) else {}

        for key, value in section("lmstudio").items():
            if hasattr(settings.lmstudio, key):
                setattr(settings.lmstudio, key, value)

        for key, value in section("mycelia").items():
            if key == "vault_path":
                value = Path(str(value))
            if hasattr(settings.mycelia, key):
                setattr(settings.mycelia, key, value)

        for key, value in section("pipeline").items():
            if key == "workspace":
                value = Path(str(value))
            if hasattr(settings.pipeline, key):
                setattr(settings.pipeline, key, value)

        for key, value in section("security").items():
            if hasattr(settings.security, key):
                setattr(settings.security, key, value)

        settings._apply_env()
        settings.normalize()
        return settings

    def normalize(self) -> None:
        # Preserve old configs that only set timeout_seconds.
        if self.lmstudio.chat_timeout_seconds <= 0:
            self.lmstudio.chat_timeout_seconds = self.lmstudio.timeout_seconds
        if self.lmstudio.embedding_timeout_seconds <= 0:
            self.lmstudio.embedding_timeout_seconds = self.lmstudio.timeout_seconds
        if self.lmstudio.gateway_timeout_seconds <= 0:
            self.lmstudio.gateway_timeout_seconds = 120.0
        if self.lmstudio.final_timeout_seconds <= 0:
            self.lmstudio.final_timeout_seconds = max(self.lmstudio.chat_timeout_seconds, 600.0)

        self.lmstudio.request_retries = max(0, int(self.lmstudio.request_retries))
        self.lmstudio.retry_backoff_seconds = max(0.1, float(self.lmstudio.retry_backoff_seconds))

        self.pipeline.max_chars_per_shard = max(500, int(self.pipeline.max_chars_per_shard))
        self.pipeline.min_chars_per_shard = max(300, int(self.pipeline.min_chars_per_shard))
        self.pipeline.shard_overlap = max(0, min(int(self.pipeline.shard_overlap), self.pipeline.max_chars_per_shard // 2))
        self.pipeline.batch_size = max(1, int(self.pipeline.batch_size))
        self.pipeline.max_embedding_batch_size = max(1, int(self.pipeline.max_embedding_batch_size))
        self.pipeline.analysis_workers = max(1, int(self.pipeline.analysis_workers))
        self.pipeline.max_analysis_workers = max(1, int(self.pipeline.max_analysis_workers))
        self.pipeline.analysis_workers = min(self.pipeline.analysis_workers, self.pipeline.max_analysis_workers)
        self.pipeline.checkpoint_every = max(1, int(self.pipeline.checkpoint_every))
        self.pipeline.chapter_context_file_limit = max(10, int(self.pipeline.chapter_context_file_limit))
        self.pipeline.chapter_context_module_limit = max(5, int(self.pipeline.chapter_context_module_limit))
        profile = str(self.pipeline.profile or "enterprise").strip().lower()
        if profile not in {"quick", "balanced", "enterprise"}:
            profile = "enterprise"
        self.pipeline.profile = profile
        self.pipeline.max_final_chapters = max(0, int(self.pipeline.max_final_chapters))

        # Profile defaults are intentionally conservative. Explicit CLI flags can still override
        # shard sizes, token budgets, retrieval limit and selected chapters.
        if profile == "quick":
            self.pipeline.single_pass_final = True
            self.pipeline.disable_module_reduce = True
            self.pipeline.retrieval_limit = min(int(self.pipeline.retrieval_limit), 4)
            self.lmstudio.max_chapter_tokens = min(int(self.lmstudio.max_chapter_tokens), 2200)
        elif profile == "balanced":
            self.pipeline.retrieval_limit = min(int(self.pipeline.retrieval_limit), 6)
            self.lmstudio.max_chapter_tokens = min(int(self.lmstudio.max_chapter_tokens), 3000)

    def _apply_env(self) -> None:
        if env := os.environ.get("LMSTUDIO_BASE_URL"):
            self.lmstudio.base_url = env
        if env := os.environ.get("LMSTUDIO_CHAT_MODEL"):
            self.lmstudio.chat_model = env
        if env := os.environ.get("LMSTUDIO_EMBEDDING_MODEL"):
            self.lmstudio.embedding_model = env
        if env := os.environ.get("DOCFORGE_CHAT_TIMEOUT"):
            self.lmstudio.chat_timeout_seconds = float(env)
        if env := os.environ.get("DOCFORGE_EMBEDDING_TIMEOUT"):
            self.lmstudio.embedding_timeout_seconds = float(env)
        if env := os.environ.get("DOCFORGE_GATEWAY_TIMEOUT"):
            self.lmstudio.gateway_timeout_seconds = float(env)
        if env := os.environ.get("DOCFORGE_LLM_RETRIES"):
            self.lmstudio.request_retries = int(env)
        if env := os.environ.get("DOCFORGE_PROFILE"):
            self.pipeline.profile = env
        if env := os.environ.get("DOCFORGE_CHAPTERS"):
            self.pipeline.chapters = env
        if env := os.environ.get("MYCELIA_BASE_URL"):
            self.mycelia.base_url = env
        token = os.environ.get(self.mycelia.token_env, "")
        if token and not self.mycelia.token:
            self.mycelia.token = token
        self.normalize()
