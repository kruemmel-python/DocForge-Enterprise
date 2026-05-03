from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


CHAPTERS = [
    "Executive Summary",
    "Systemüberblick",
    "Architektur",
    "Modulübersicht",
    "Datenflüsse",
    "Schnittstellen und APIs",
    "Konfigurationsmodell",
    "Sicherheitsbetrachtung",
    "Betrieb, Deployment und Observability",
    "Risiken und technische Schulden",
    "Erweiterungspunkte",
    "Glossar",
    "Anhang: Dateiübersicht und Evidenz",
]


BALANCED_CHAPTERS = [
    "Executive Summary",
    "Systemüberblick",
    "Architektur",
    "Modulübersicht",
    "Schnittstellen und APIs",
    "Sicherheitsbetrachtung",
    "Risiken und technische Schulden",
    "Anhang: Dateiübersicht und Evidenz",
]

QUICK_CHAPTERS = [
    "Executive Summary",
    "Systemüberblick",
    "Sicherheitsbetrachtung",
    "Anhang: Dateiübersicht und Evidenz",
]


def chapters_for_profile(profile: str, chapters_csv: str = "", max_chapters: int = 0) -> list[str]:
    """Return the chapter plan for the selected documentation profile."""
    if chapters_csv.strip():
        chapters = [item.strip() for item in chapters_csv.split(",") if item.strip()]
    else:
        match (profile or "enterprise").lower():
            case "quick":
                chapters = list(QUICK_CHAPTERS)
            case "balanced":
                chapters = list(BALANCED_CHAPTERS)
            case _:
                chapters = list(CHAPTERS)
    if max_chapters > 0:
        chapters = chapters[:max_chapters]
    return chapters


def fallback_chapter(
    title: str,
    *,
    project_name: str,
    module_summaries: list[dict[str, Any]],
    file_summaries: list[dict[str, Any]],
) -> str:
    if title == "Executive Summary":
        return (
            f"## {title}\n\n"
            f"`{project_name}` wurde automatisiert analysiert. "
            f"Die Dokumentation basiert auf {len(file_summaries)} Datei-Zusammenfassungen "
            f"und {len(module_summaries)} Modul-Zusammenfassungen. "
            "Dieser Abschnitt wurde im Dry-Run- oder Fallback-Modus erzeugt.\n"
        )

    if title == "Modulübersicht":
        lines = [f"## {title}", ""]
        for module in module_summaries:
            lines.append(f"### {module.get('module_name', 'unknown')}")
            lines.append(str(module.get("responsibility", "Keine Beschreibung verfügbar.")))
            files = module.get("files", [])
            if files:
                lines.append("")
                lines.append("Dateien: " + ", ".join(map(str, files[:20])))
            lines.append("")
        return "\n".join(lines)

    if title.startswith("Anhang"):
        lines = [f"## {title}", ""]
        for item in sorted(file_summaries, key=lambda x: str(x.get("file_path", ""))):
            lines.append(f"- `{item.get('file_path', '')}` — {item.get('purpose', '')}")
        return "\n".join(lines) + "\n"

    return f"## {title}\n\nKeine detaillierten Inhalte im Fallback-Modus verfügbar.\n"


def assemble_markdown(
    *,
    project_name: str,
    chapters: list[str],
    metadata: dict[str, Any],
) -> str:
    front = [
        f"# Enterprise-Dokumentation: {project_name}",
        "",
        "> Automatisch generiert durch DocForge Enterprise.",
        "",
        "## Dokument-Metadaten",
        "",
        "```json",
        json.dumps(metadata, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(front + chapters) + "\n"


def markdown_to_simple_html(markdown: str, *, title: str) -> str:
    # Minimaler HTML-Renderer ohne externe Abhängigkeiten. Markdown bleibt in <pre>
    # erhalten, damit keine unsichere HTML-Injektion aus Modellantworten ausgeführt wird.
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 3rem; line-height: 1.55; }}
    pre {{ white-space: pre-wrap; background: #f6f8fa; padding: 1rem; border-radius: 12px; }}
  </style>
</head>
<body>
<pre>{html.escape(markdown)}</pre>
</body>
</html>
"""


def write_outputs(
    output_dir: Path,
    *,
    project_name: str,
    markdown: str,
    metadata: dict[str, Any],
    emit_html: bool,
    emit_json: bool,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    md_path = output_dir / "enterprise_documentation.md"
    md_path.write_text(markdown, encoding="utf-8")
    paths["markdown"] = str(md_path)

    if emit_html:
        html_path = output_dir / "enterprise_documentation.html"
        html_path.write_text(
            markdown_to_simple_html(markdown, title=f"Enterprise-Dokumentation: {project_name}"),
            encoding="utf-8",
        )
        paths["html"] = str(html_path)

    if emit_json:
        json_path = output_dir / "run_metadata.json"
        json_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        paths["metadata"] = str(json_path)

    return paths
