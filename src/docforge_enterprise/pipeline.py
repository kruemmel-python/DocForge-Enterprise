from __future__ import annotations
import json, shutil, time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from .audit import validate_claims, append_audit_section
from .config import Settings
from .extractor import prepare_input, iter_project_files
from .hashing import stable_id
from .lmstudio import LMStudioChatClient, LLMError
from .models import AnalysisRecord, CodeShard, ProjectFile, PipelineStats, RetrievedContext
from .prompts import SHARD_SYSTEM, FILE_SYSTEM, MODULE_SYSTEM, CHAPTER_SYSTEM, shard_prompt, file_prompt, module_prompt, chapter_prompt, one_pass_document_prompt
from .renderer import chapters_for_profile, fallback_chapter, assemble_markdown, write_outputs
from .semantic_index import SemanticIndex
from .sharding import ShardPlan, shard_project
from .store import AnalysisStore
@dataclass(slots=True)
class PipelineResult:
    output_paths:dict[str,str]; metadata:dict[str,Any]; workspace:Path
class DocumentationPipeline:
    def __init__(self,*,input_path:Path,settings:Settings):
        self.input_path=input_path; self.settings=settings; self.settings.normalize(); self.workspace=settings.pipeline.workspace
        if settings.pipeline.force_rebuild and self.workspace.exists():
            shutil.rmtree(self.workspace)
        self.project_name=settings.pipeline.project_name or input_path.stem
        self.stats=PipelineStats(); self.store=AnalysisStore(self.workspace/"analysis/docforge.sqlite3")
        self.chat=LMStudioChatClient(settings.lmstudio); self.index=SemanticIndex(settings,f"{settings.mycelia.collection_prefix}_code")
    def close(self): self.store.close()
    def _chapter_plan(self): return chapters_for_profile(self.settings.pipeline.profile,self.settings.pipeline.chapters,self.settings.pipeline.max_final_chapters)
    def _estimate(self,files,shards,chapters):
        modules=len({self._module_name_for(f.relative_path) for f in files}); batches=(len(shards)+max(1,self.settings.pipeline.max_embedding_batch_size)-1)//max(1,self.settings.pipeline.max_embedding_batch_size)
        if self.settings.pipeline.dry_run: shard=file=module=render=ret=0
        else:
            shard=len(shards); file=len(files); module=0 if self.settings.pipeline.disable_module_reduce else modules; render=1 if self.settings.pipeline.single_pass_final else len(chapters); ret=1 if self.settings.pipeline.single_pass_final else len(chapters)
        return {"profile":self.settings.pipeline.profile,"files":len(files),"shards":len(shards),"modules":modules,"chapters":len(chapters),"estimated_shard_analysis_calls":shard,"estimated_file_reduce_calls":file,"estimated_module_reduce_calls":module,"estimated_chapter_render_calls":render,"estimated_embedding_ingest_batches":batches,"estimated_retrieval_embedding_calls":ret,"estimated_llm_chat_calls":shard+file+module+render,"estimated_embedding_calls":batches+ret}
    def run(self)->PipelineResult:
        started=time.time(); extracted=prepare_input(self.input_path,self.workspace,self.settings)
        files=list(iter_project_files(extracted,self.settings)); self.stats.files_seen=len(files); self.stats.files_indexed=len(files); self.store.upsert_files(files)
        shards=shard_project(files,ShardPlan(self.settings.pipeline.max_chars_per_shard,self.settings.pipeline.shard_overlap)); self.stats.shards_created=len(shards); self.store.upsert_shards(shards)
        chapters_plan=self._chapter_plan(); estimate=self._estimate(files,shards,chapters_plan); self.stats.estimated_llm_chat_calls=estimate["estimated_llm_chat_calls"]; self.stats.estimated_embedding_calls=estimate["estimated_embedding_calls"]
        print("[DocForge] Work estimate:", json.dumps(estimate,ensure_ascii=False))
        if self.settings.pipeline.estimate_only:
            metadata=self._metadata(started,[],estimate,chapters_plan,{}); md=assemble_markdown(project_name=self.project_name,chapters=["## Work Estimate\n\n```json\n"+json.dumps(estimate,ensure_ascii=False,indent=2)+"\n```"],metadata=metadata)
            paths=write_outputs(self.workspace/"output",project_name=self.project_name,markdown=md,metadata=metadata,emit_html=True,emit_json=True); return PipelineResult(paths,metadata,self.workspace)
        ingest=self.index.ingest(shards,batch_size=self.settings.pipeline.batch_size); self.stats.actual_embedding_calls += max(1, estimate["estimated_embedding_ingest_batches"])
        for shard in shards: self._analyze_shard(shard)
        file_summaries=self._reduce_files(files); module_summaries=self._reduce_modules(file_summaries)
        final=self._generate_final(file_summaries,module_summaries,chapters_plan)
        records=self.store.list_analysis("shard")+file_summaries+module_summaries
        source_map={f.relative_path:f.content for f in files}; audit_report=validate_claims(records,source_map) if self.settings.audit.validate_claims else {}
        if audit_report:
            self.stats.claims_total=audit_report["claims_total"]; self.stats.claims_supported=audit_report["claims_supported"]; self.stats.claims_unsupported=audit_report["claims_unsupported"]; self.stats.evidence_coverage_percent=audit_report["evidence_coverage_percent"]
            final=append_audit_section(final,audit_report)
        metadata=self._metadata(started,ingest,estimate,chapters_plan,audit_report)
        paths=write_outputs(self.workspace/"output",project_name=self.project_name,markdown=final,metadata=metadata,emit_html=True,emit_json=True)
        (self.workspace/"analysis").mkdir(parents=True,exist_ok=True)
        (self.workspace/"analysis/files.json").write_text(json.dumps([asdict(f)|{"path":str(f.path)} for f in files],ensure_ascii=False,indent=2),encoding="utf-8")
        (self.workspace/"analysis/shards.json").write_text(json.dumps([asdict(s) for s in shards],ensure_ascii=False,indent=2),encoding="utf-8")
        (self.workspace/"analysis/audit_validation.json").write_text(json.dumps(audit_report,ensure_ascii=False,indent=2),encoding="utf-8")
        return PipelineResult(paths,metadata,self.workspace)
    def _metadata(self,started,ingest,estimate,chapters,audit_report):
        return {"project_name":self.project_name,"input":str(self.input_path),"workspace":str(self.workspace),"profile":self.settings.pipeline.profile,"selected_chapters":chapters,"work_estimate":estimate,"audit_validation":audit_report,"started_at":started,"finished_at":time.time(),"duration_seconds":round(time.time()-started,3),"stats":asdict(self.stats),"ingest_results":ingest,"lmstudio":{"base_url":self.settings.lmstudio.base_url,"chat_model":self.settings.lmstudio.chat_model,"embedding_model":self.settings.lmstudio.embedding_model}}
    def _retrieve(self,query,target_id,limit=None):
        if self.settings.pipeline.dry_run: return []
        ctx,meta=self.index.query(query,limit=limit); self.stats.retrieval_events+=1; self.stats.actual_embedding_calls+=1; self.store.save_retrieval_event(query=query,target_id=target_id,metadata=meta); return [c for c in ctx if c.id!=target_id]
    def _analyze_shard(self,shard:CodeShard):
        ctx=self._retrieve(f"{shard.file_path} {' '.join(shard.symbols)} {shard.content[:400]}",shard.id)
        if self.settings.pipeline.dry_run: payload={"file_path":shard.file_path,"shard_id":shard.id,"purpose":"Dry-run shard summary","important_symbols":list(shard.symbols),"dependencies":[],"business_rules":[],"interfaces":[],"security_notes":[],"operations_notes":[],"risks":["Dry-run"],"documentation_notes":[],"evidence":[{"file_path":shard.file_path,"span":f"{shard.char_start}-{shard.char_end}","claim":"Shard indexed."}]}
        else:
            try: payload=self.chat.chat_json(system=SHARD_SYSTEM,user=shard_prompt(shard,ctx),max_tokens=self.settings.lmstudio.max_json_tokens); self.stats.actual_llm_chat_calls+=1
            except LLMError as e: self.stats.llm_failures+=1; payload={"file_path":shard.file_path,"shard_id":shard.id,"purpose":"LLM failed","important_symbols":list(shard.symbols),"dependencies":[],"business_rules":[],"interfaces":[],"security_notes":[],"operations_notes":[],"risks":[str(e)],"documentation_notes":[],"evidence":[]}
        self.store.save_analysis(AnalysisRecord(stable_id("shard",shard.id,shard.sha256),"shard",shard.id,payload)); self.stats.shards_analyzed+=1; return payload
    def _reduce_files(self,files:list[ProjectFile]):
        out=[]
        for f in files:
            shard_records=self.store.list_analysis("shard")
            relevant=[r for r in shard_records if r.get("file_path")==f.relative_path]
            if self.settings.pipeline.dry_run: payload={"file_path":f.relative_path,"purpose":"Dry-run file summary","public_api":[],"internal_logic":[],"dependencies":[],"business_rules":[],"interfaces":[],"security_notes":[],"operations_notes":[],"risks":["Dry-run"],"enterprise_notes":[],"evidence":[{"file_path":f.relative_path,"claim":"File indexed."}]}
            else:
                try: payload=self.chat.chat_json(system=FILE_SYSTEM,user=file_prompt(f.relative_path,relevant),max_tokens=self.settings.lmstudio.max_json_tokens); self.stats.actual_llm_chat_calls+=1
                except LLMError as e: self.stats.llm_failures+=1; payload={"file_path":f.relative_path,"purpose":"File reduction failed","public_api":[],"internal_logic":[],"dependencies":[],"business_rules":[],"interfaces":[],"security_notes":[],"operations_notes":[],"risks":[str(e)],"enterprise_notes":[],"evidence":[]}
            self.store.save_analysis(AnalysisRecord(stable_id("file",f.relative_path,f.sha256),"file",f.relative_path,payload)); out.append(payload)
        return out
    def _module_name_for(self,path): return Path(path).parts[0] if len(Path(path).parts)>1 else "root"
    def _reduce_modules(self,files:list[dict]):
        groups={}
        for f in files: groups.setdefault(self._module_name_for(str(f.get("file_path","root"))),[]).append(f)
        out=[]
        for name,summaries in groups.items():
            if self.settings.pipeline.disable_module_reduce or self.settings.pipeline.dry_run:
                payload={"module_name":name,"responsibility":"Structural module summary","files":[s.get("file_path","") for s in summaries],"main_flows":[],"dependencies":[],"interfaces":[],"security_notes":[],"operations_notes":[],"risks":["LLM module reduction disabled."],"evidence":[]}
            else:
                try: payload=self.chat.chat_json(system=MODULE_SYSTEM,user=module_prompt(name,summaries),max_tokens=self.settings.lmstudio.max_json_tokens); self.stats.actual_llm_chat_calls+=1
                except LLMError as e: self.stats.llm_failures+=1; payload={"module_name":name,"responsibility":"Module reduction failed","files":[s.get("file_path","") for s in summaries],"main_flows":[],"dependencies":[],"interfaces":[],"security_notes":[],"operations_notes":[],"risks":[str(e)],"evidence":[]}
            self.store.save_analysis(AnalysisRecord(stable_id("module",name,json.dumps(summaries,sort_keys=True)),"module",name,payload)); out.append(payload)
        return out
    def _generate_final(self,files,modules,chapters):
        if self.settings.pipeline.dry_run: parts=[fallback_chapter(c,project_name=self.project_name,module_summaries=modules,file_summaries=files) for c in chapters]; return assemble_markdown(project_name=self.project_name,chapters=parts,metadata={})
        if self.settings.pipeline.single_pass_final:
            ctx=self._retrieve(f"{self.project_name} quick documentation architecture security","final")
            try: text=self.chat.chat(system=CHAPTER_SYSTEM,user=one_pass_document_prompt(self.project_name,chapters,modules,files,ctx),max_tokens=self.settings.lmstudio.max_chapter_tokens,timeout=self.settings.lmstudio.final_timeout_seconds); self.stats.actual_llm_chat_calls+=1; return text
            except LLMError: pass
        parts=[]
        for c in chapters:
            ctx=self._retrieve(f"{self.project_name} {c} architecture security operations interfaces configuration",f"chapter:{c}")
            try: t=self.chat.chat(system=CHAPTER_SYSTEM,user=chapter_prompt(self.project_name,c,modules,files,ctx),max_tokens=self.settings.lmstudio.max_chapter_tokens,timeout=self.settings.lmstudio.final_timeout_seconds); self.stats.actual_llm_chat_calls+=1
            except LLMError: t=fallback_chapter(c,project_name=self.project_name,module_summaries=modules,file_summaries=files)
            parts.append(t if t.lstrip().startswith("#") else f"## {c}\n\n{t}\n")
        return assemble_markdown(project_name=self.project_name,chapters=parts,metadata={})
