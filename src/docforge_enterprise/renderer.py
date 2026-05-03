from __future__ import annotations
import html, json
from pathlib import Path
from typing import Any
CHAPTERS=["Executive Summary","Systemüberblick","Architektur","Modulübersicht","Datenflüsse","Schnittstellen und APIs","Konfigurationsmodell","Sicherheitsbetrachtung","Betrieb, Deployment und Observability","Risiken und technische Schulden","Erweiterungspunkte","Glossar","Anhang: Dateiübersicht und Evidenz"]
BALANCED=["Executive Summary","Systemüberblick","Architektur","Modulübersicht","Schnittstellen und APIs","Sicherheitsbetrachtung","Risiken und technische Schulden","Anhang: Dateiübersicht und Evidenz"]
QUICK=["Executive Summary","Systemüberblick","Sicherheitsbetrachtung","Anhang: Dateiübersicht und Evidenz"]
def chapters_for_profile(profile:str, csv:str="", max_chapters:int=0)->list[str]:
    chapters=[x.strip() for x in csv.split(",") if x.strip()] if csv.strip() else (QUICK if profile=="quick" else BALANCED if profile=="balanced" else CHAPTERS)
    return chapters[:max_chapters] if max_chapters>0 else list(chapters)
def fallback_chapter(title:str,*,project_name:str,module_summaries:list[dict],file_summaries:list[dict])->str:
    if title.startswith("Anhang"):
        return "## "+title+"\n\n"+"\n".join(f"- `{f.get('file_path','')}` — {f.get('purpose','')}" for f in file_summaries)+"\n"
    return f"## {title}\n\nFallback-Dokumentation für `{project_name}`. Dateien: {len(file_summaries)}, Module: {len(module_summaries)}.\n"
def assemble_markdown(*,project_name:str,chapters:list[str],metadata:dict[str,Any])->str:
    return f"# Enterprise-Dokumentation: {project_name}\n\n## Metadaten\n\n```json\n{json.dumps(metadata,ensure_ascii=False,indent=2)}\n```\n\n"+"\n\n".join(chapters)+"\n"
def markdown_to_simple_html(md:str,*,title:str)->str:
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title><style>body{{font-family:system-ui;margin:3rem;line-height:1.55}}pre{{white-space:pre-wrap;background:#f6f8fa;padding:1rem;border-radius:12px}}</style></head><body><pre>{html.escape(md)}</pre></body></html>"
def write_outputs(output_dir:Path,*,project_name:str,markdown:str,metadata:dict[str,Any],emit_html:bool,emit_json:bool)->dict[str,str]:
    output_dir.mkdir(parents=True,exist_ok=True); paths={}
    md=output_dir/"enterprise_documentation.md"; md.write_text(markdown,encoding="utf-8"); paths["markdown"]=str(md)
    if emit_html:
        hp=output_dir/"enterprise_documentation.html"; hp.write_text(markdown_to_simple_html(markdown,title=project_name),encoding="utf-8"); paths["html"]=str(hp)
    if emit_json:
        jp=output_dir/"run_metadata.json"; jp.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding="utf-8"); paths["metadata"]=str(jp)
    return paths
