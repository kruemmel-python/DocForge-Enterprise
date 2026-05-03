from __future__ import annotations
import json
from .models import CodeShard, RetrievedContext
SHARD_SYSTEM="You are a Senior Software Architect. Return only valid JSON with purpose, important_symbols, dependencies, business_rules, interfaces, security_notes, operations_notes, risks, documentation_notes, evidence. Follow the requested output language for all natural-language values."
FILE_SYSTEM="Reduce shard analyses into file documentation. Return JSON only. Follow the requested output language for all natural-language values."
MODULE_SYSTEM="Reduce file summaries into module documentation. Return JSON only. Follow the requested output language for all natural-language values."
CHAPTER_SYSTEM="You are a Principal Enterprise Architect. Write professional Markdown documentation. Follow the requested output language. Mark unsupported claims explicitly."
def _ctx(ctxs:list[RetrievedContext], n:int=1600)->str:
    return "\n\n".join(f"CONTEXT {i} FILE={c.file_path} SCORE={c.score}\n{c.text[:n]}" for i,c in enumerate(ctxs,1))
def shard_prompt(s:CodeShard, ctxs:list[RetrievedContext])->str:
    return f"SHARD {s.id} FILE={s.file_path} SPAN={s.char_start}-{s.char_end} SYMBOLS={s.symbols}\n```{s.language}\n{s.content}\n```\nRELATED:\n{_ctx(ctxs)}"
def file_prompt(path:str, analyses:list[dict])->str:
    return f"Datei {path}\nSHARD_ANALYSES:\n{json.dumps(analyses,ensure_ascii=False,indent=2)}"
def module_prompt(name:str, summaries:list[dict])->str:
    return f"Modul {name}\nFILE_SUMMARIES:\n{json.dumps(summaries,ensure_ascii=False,indent=2)}"
def chapter_prompt(project:str,title:str,modules:list[dict],files:list[dict],ctxs:list[RetrievedContext])->str:
    return f"Projekt {project}\nKapitel {title}\nMODULES:{json.dumps(modules,ensure_ascii=False,indent=2)}\nFILES:{json.dumps(files,ensure_ascii=False,indent=2)}\nCTX:\n{_ctx(ctxs)}"
def one_pass_document_prompt(project:str, chapters:list[str], modules:list[dict], files:list[dict], ctxs:list[RetrievedContext])->str:
    return f"Projekt {project}\nErstelle Markdown mit Kapiteln {chapters}.\nMODULES:{json.dumps(modules,ensure_ascii=False,indent=2)}\nFILES:{json.dumps(files,ensure_ascii=False,indent=2)}\nCTX:\n{_ctx(ctxs)}"
