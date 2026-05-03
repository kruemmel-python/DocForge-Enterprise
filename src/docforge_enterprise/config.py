from __future__ import annotations
import os, tomllib
from dataclasses import dataclass, field
from pathlib import Path
@dataclass(slots=True)
class LMStudioSettings:
    base_url:str="http://127.0.0.1:1234/v1"; chat_model:str="google_gemma-4-e4b-it"; embedding_model:str="text-embedding-nomic-embed-text-v2-moe"
    chat_timeout_seconds:float=600.0; embedding_timeout_seconds:float=300.0; gateway_timeout_seconds:float=180.0; final_timeout_seconds:float=900.0
    request_retries:int=3; retry_backoff_seconds:float=2.0; temperature:float=0.1; max_json_tokens:int=900; max_chapter_tokens:int=3500; json_repair_attempts:int=1
@dataclass(slots=True)
class MyceliaSettings:
    enabled:bool=True; base_url:str="http://127.0.0.1:9999"; token:str=""; token_env:str="MYCELIA_LOCAL_TOKEN"; vault_path:Path=Path(".docforge_workspace/mycelia_vault")
    collection_prefix:str="docforge"; search_backend:str="auto"; store_text:bool=True; default_dimension:int=768
@dataclass(slots=True)
class PipelineSettings:
    workspace:Path=Path(".docforge_workspace"); max_chars_per_shard:int=2500; min_chars_per_shard:int=1000; shard_overlap:int=300
    retrieval_limit:int=6; batch_size:int=8; max_embedding_batch_size:int=4; analysis_workers:int=1; max_analysis_workers:int=2
    dry_run:bool=False; force_rebuild:bool=False; emit_html:bool=True; emit_json:bool=True; project_name:str=""; output_language:str="de"
    profile:str="balanced"; chapters:str=""; single_pass_final:bool=False; disable_module_reduce:bool=False; max_final_chapters:int=0; estimate_only:bool=False
    checkpoint_every:int=1; adaptive_shard_on_timeout:bool=True; continue_on_timeout:bool=True; fail_on_missing_shards:bool=True; integrity_debug:bool=True
@dataclass(slots=True)
class SecuritySettings:
    max_file_bytes:int=2_000_000; block_secret_files:bool=True; block_vendor_dirs:bool=True; allow_binary:bool=False; redact_secrets:bool=True; fail_on_zip_slip:bool=True
@dataclass(slots=True)
class WebGUISettings:
    auth_required:bool=True; allow_registration:bool=True; first_user_admin:bool=True; session_ttl_seconds:int=28800; csrf_required:bool=True
    max_upload_bytes:int=100_000_000; read_only:bool=False; bind_host:str="127.0.0.1"; port:int=7860
@dataclass(slots=True)
class AuditSettings:
    validate_claims:bool=True; mark_unsupported_claims:bool=True; min_evidence_coverage_percent:float=60.0; require_review_for_final:bool=False
@dataclass(slots=True)
class Settings:
    lmstudio:LMStudioSettings=field(default_factory=LMStudioSettings); mycelia:MyceliaSettings=field(default_factory=MyceliaSettings)
    pipeline:PipelineSettings=field(default_factory=PipelineSettings); security:SecuritySettings=field(default_factory=SecuritySettings)
    webgui:WebGUISettings=field(default_factory=WebGUISettings); audit:AuditSettings=field(default_factory=AuditSettings)
    @classmethod
    def from_toml(cls, path:Path|None)->"Settings":
        s=cls()
        if path and path.exists():
            data=tomllib.loads(path.read_text(encoding="utf-8"))
            for section,obj in [("lmstudio",s.lmstudio),("mycelia",s.mycelia),("pipeline",s.pipeline),("security",s.security),("webgui",s.webgui),("audit",s.audit)]:
                raw=data.get(section,{})
                if isinstance(raw,dict):
                    for k,v in raw.items():
                        if hasattr(obj,k):
                            if k in {"workspace","vault_path"}: v=Path(str(v))
                            setattr(obj,k,v)
        s._apply_env(); s.normalize(); return s
    def _apply_env(self)->None:
        if os.getenv("LMSTUDIO_BASE_URL"): self.lmstudio.base_url=os.environ["LMSTUDIO_BASE_URL"]
        if os.getenv("LMSTUDIO_CHAT_MODEL"): self.lmstudio.chat_model=os.environ["LMSTUDIO_CHAT_MODEL"]
        if os.getenv("LMSTUDIO_EMBEDDING_MODEL"): self.lmstudio.embedding_model=os.environ["LMSTUDIO_EMBEDDING_MODEL"]
        if os.getenv("DOCFORGE_OUTPUT_LANGUAGE"): self.pipeline.output_language=os.environ["DOCFORGE_OUTPUT_LANGUAGE"]
        if os.getenv("MYCELIA_BASE_URL"): self.mycelia.base_url=os.environ["MYCELIA_BASE_URL"]
        tok=os.getenv(self.mycelia.token_env,"")
        if tok and not self.mycelia.token: self.mycelia.token=tok
    def normalize(self)->None:
        self.pipeline.profile=(self.pipeline.profile or "balanced").lower()
        if self.pipeline.profile not in {"quick","balanced","enterprise"}: self.pipeline.profile="balanced"
        if self.pipeline.profile=="quick":
            self.pipeline.single_pass_final=True; self.pipeline.disable_module_reduce=True; self.pipeline.retrieval_limit=min(self.pipeline.retrieval_limit,4)
        elif self.pipeline.profile=="balanced":
            self.pipeline.retrieval_limit=min(self.pipeline.retrieval_limit,6)
        self.pipeline.analysis_workers=max(1,min(int(self.pipeline.analysis_workers), max(1,int(self.pipeline.max_analysis_workers))))
        self.pipeline.max_chars_per_shard=max(500,int(self.pipeline.max_chars_per_shard))
