from __future__ import annotations

import json

from .models import CodeShard, RetrievedContext


SHARD_SYSTEM = """
Du bist ein Senior Enterprise Software Architect.
Analysiere kleine Code- oder Dokumentationsausschnitte für eine auditierbare Enterprise-Dokumentation.
Antworte ausschließlich als valides JSON ohne Markdown.

Schema:
{
  "file_path": "string",
  "shard_id": "string",
  "purpose": "string",
  "important_symbols": ["string"],
  "dependencies": ["string"],
  "business_rules": ["string"],
  "interfaces": ["string"],
  "security_notes": ["string"],
  "operations_notes": ["string"],
  "risks": ["string"],
  "documentation_notes": ["string"],
  "evidence": [{"file_path": "string", "span": "start-end", "claim": "string"}]
}
""".strip()


FILE_SYSTEM = """
Du bist ein Enterprise Documentation Engineer.
Verdichte Shard-Analysen zu einer Datei-Dokumentation.
Antworte ausschließlich als valides JSON ohne Markdown.

Schema:
{
  "file_path": "string",
  "purpose": "string",
  "public_api": ["string"],
  "internal_logic": ["string"],
  "dependencies": ["string"],
  "business_rules": ["string"],
  "interfaces": ["string"],
  "security_notes": ["string"],
  "operations_notes": ["string"],
  "risks": ["string"],
  "enterprise_notes": ["string"],
  "evidence": [{"file_path": "string", "claim": "string"}]
}
""".strip()


MODULE_SYSTEM = """
Du bist ein Principal Solution Architect.
Verdichte Datei-Zusammenfassungen zu Modul-Dokumentation.
Antworte ausschließlich als valides JSON ohne Markdown.

Schema:
{
  "module_name": "string",
  "responsibility": "string",
  "files": ["string"],
  "main_flows": ["string"],
  "dependencies": ["string"],
  "interfaces": ["string"],
  "security_notes": ["string"],
  "operations_notes": ["string"],
  "risks": ["string"],
  "evidence": [{"file_path": "string", "claim": "string"}]
}
""".strip()


CHAPTER_SYSTEM = """
Du bist ein Principal Enterprise Architect und Technical Writer.
Erstelle professionelle deutschsprachige Enterprise-Dokumentation.
Arbeite belegorientiert: keine Behauptung ohne Bezug auf die gelieferten Datei-, Modul- oder Retrieval-Kontexte.
Schreibe in Markdown.
""".strip()


def _context_block(contexts: list[RetrievedContext], max_chars_each: int = 2200) -> str:
    blocks: list[str] = []
    for i, ctx in enumerate(contexts, start=1):
        blocks.append(
            f"""
RELATED_CONTEXT {i}
ID: {ctx.id}
FILE: {ctx.file_path}
SCORE: {ctx.score:.4f}
METADATA: {json.dumps(ctx.metadata, ensure_ascii=False)}
TEXT:
{ctx.text[:max_chars_each]}
""".strip()
        )
    return "\n\n".join(blocks)


def shard_prompt(shard: CodeShard, contexts: list[RetrievedContext]) -> str:
    return f"""
Analysiere den aktuellen Shard und berücksichtige die semantisch verwandten Projektkontexte.

CURRENT_SHARD
ID: {shard.id}
FILE: {shard.file_path}
LANGUAGE: {shard.language}
KIND: {shard.kind}
SPAN: {shard.char_start}-{shard.char_end}
SYMBOLS: {", ".join(shard.symbols)}

CODE_OR_TEXT:
```{shard.language}
{shard.content}
```

SEMANTIC_RETRIEVAL_CONTEXTS:
{_context_block(contexts)}

Ziel:
- Zweck und Rolle im Gesamtsystem erkennen.
- Schnittstellen, Abhängigkeiten, Business-Regeln, Sicherheits- und Betriebsaspekte extrahieren.
- Keine erfundenen Komponenten nennen.
- Unsicherheiten als Risiko oder Dokumentationshinweis markieren.
""".strip()


def file_prompt(file_path: str, shard_analyses: list[dict]) -> str:
    return f"""
Datei: {file_path}

Verdichte diese Shard-Analysen zu einer Datei-Dokumentation.
Entferne Dopplungen, markiere Unsicherheiten und erhalte belegbare Details.

SHARD_ANALYSES:
{json.dumps(shard_analyses, ensure_ascii=False, indent=2)}
""".strip()


def module_prompt(module_name: str, file_summaries: list[dict]) -> str:
    return f"""
Modul: {module_name}

Verdichte diese Datei-Zusammenfassungen zu einer Modulbeschreibung.

FILE_SUMMARIES:
{json.dumps(file_summaries, ensure_ascii=False, indent=2)}
""".strip()


def chapter_prompt(
    project_name: str,
    chapter_title: str,
    module_summaries: list[dict],
    file_summaries: list[dict],
    extra_contexts: list[RetrievedContext],
) -> str:
    return f"""
Projekt: {project_name}
Kapitel: {chapter_title}

Erstelle nur dieses Kapitel als Markdown-Abschnitt.
Nutze konkrete Datei- und Modulnamen.
Schreibe professionell, präzise und enterprise-tauglich.

MODULE_SUMMARIES:
{json.dumps(module_summaries, ensure_ascii=False, indent=2)}

FILE_SUMMARIES:
{json.dumps(file_summaries, ensure_ascii=False, indent=2)}

RETRIEVAL_CONTEXTS:
{_context_block(extra_contexts, max_chars_each=1800)}
""".strip()



def one_pass_document_prompt(
    project_name: str,
    chapter_titles: list[str],
    module_summaries: list[dict],
    file_summaries: list[dict],
    extra_contexts: list[RetrievedContext],
) -> str:
    return f"""
Projekt: {project_name}
Profil: Quick / Single-Pass

Erstelle eine kompakte, aber professionelle Enterprise-Dokumentation als Markdown.
Nutze genau diese Kapitel in dieser Reihenfolge:
{json.dumps(chapter_titles, ensure_ascii=False, indent=2)}

Anforderungen:
- Keine erfundenen Komponenten.
- Verwende konkrete Datei- und Modulnamen.
- Markiere Unsicherheiten explizit.
- Sicherheitsrisiken und technische Schulden klar benennen.
- Für kleine Projekte kurz bleiben, für größere Projekte verdichten.

MODULE_SUMMARIES:
{json.dumps(module_summaries, ensure_ascii=False, indent=2)}

FILE_SUMMARIES:
{json.dumps(file_summaries, ensure_ascii=False, indent=2)}

RETRIEVAL_CONTEXTS:
{_context_block(extra_contexts, max_chars_each=1600)}
""".strip()
