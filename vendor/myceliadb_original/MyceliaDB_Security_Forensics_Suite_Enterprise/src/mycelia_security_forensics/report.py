from __future__ import annotations

from pathlib import Path
import json
from collections import Counter
from datetime import datetime

from .findings import Finding, SuiteReport, Status


def summarize(findings: list[Finding]) -> dict:
    by_status = Counter(f.status.value for f in findings)
    by_severity = Counter(f.severity.value for f in findings)
    hard_fail = any(f.status == Status.FAIL and f.severity.value in ("high", "critical") for f in findings)
    return {
        "total": len(findings),
        "by_status": dict(by_status),
        "by_severity": dict(by_severity),
        "enterprise_gate": "fail" if hard_fail else ("warn" if by_status.get("warn") else "pass"),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


def write_json(report: SuiteReport, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def write_markdown(report: SuiteReport, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    d = report.to_dict()
    lines.append("# MyceliaDB Enterprise Security Forensics Report\n")
    lines.append(f"- Suite: `{report.suite}`")
    lines.append(f"- Version: `{report.version}`")
    lines.append(f"- Started: `{report.started_at}`")
    lines.append(f"- Duration: `{report.duration_ms:.1f} ms`")
    lines.append(f"- Enterprise gate: **{report.summary.get('enterprise_gate')}**\n")
    lines.append("## Summary\n")
    lines.append("```json")
    lines.append(json.dumps(report.summary, indent=2, ensure_ascii=False))
    lines.append("```\n")
    lines.append("## Findings\n")
    for f in report.findings:
        icon = {"pass": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️", "skip": "⏭️"}.get(f.status.value, "•")
        lines.append(f"### {icon} {f.check_id} — {f.title}\n")
        lines.append(f"- Status: `{f.status.value}`")
        lines.append(f"- Severity: `{f.severity.value}`")
        lines.append(f"- Category: `{f.category}`")
        lines.append(f"- Duration: `{f.duration_ms:.1f} ms`")
        if f.summary:
            lines.append(f"- Summary: {f.summary}")
        if f.recommendation:
            lines.append(f"- Recommendation: {f.recommendation}")
        if f.evidence:
            lines.append("\nEvidence:")
            lines.append("```json")
            lines.append(json.dumps(f.evidence, indent=2, ensure_ascii=False)[:12000])
            lines.append("```")
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")


def write_html(report: SuiteReport, path: str) -> None:
    import html
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    cards = []
    for f in report.findings:
        cls = f.status.value
        cards.append(f"""
        <section class="finding {cls}">
          <h2>{html.escape(f.check_id)} — {html.escape(f.title)}</h2>
          <p><b>Status:</b> {html.escape(f.status.value)} · <b>Severity:</b> {html.escape(f.severity.value)} · <b>Category:</b> {html.escape(f.category)}</p>
          <p>{html.escape(f.summary)}</p>
          <details><summary>Evidence</summary><pre>{html.escape(json.dumps(f.evidence, indent=2, ensure_ascii=False)[:20000])}</pre></details>
        </section>
        """)
    content = f"""<!doctype html>
<html lang="de">
<meta charset="utf-8">
<title>MyceliaDB Forensics Report</title>
<style>
body {{ font-family: system-ui, Segoe UI, sans-serif; margin: 2rem; background:#0f172a; color:#e5e7eb; }}
h1 {{ color:#fff; }}
.finding {{ border:1px solid #334155; border-radius:16px; padding:1rem; margin:1rem 0; background:#111827; }}
.pass {{ border-left:8px solid #22c55e; }}
.warn {{ border-left:8px solid #f59e0b; }}
.fail {{ border-left:8px solid #ef4444; }}
.skip,.info {{ border-left:8px solid #38bdf8; }}
pre {{ white-space:pre-wrap; overflow:auto; background:#020617; padding:1rem; border-radius:12px; }}
</style>
<h1>MyceliaDB Enterprise Security Forensics Report</h1>
<p>Gate: <b>{html.escape(str(report.summary.get('enterprise_gate')))}</b> · Version: {html.escape(report.version)}</p>
<pre>{html.escape(json.dumps(report.summary, indent=2, ensure_ascii=False))}</pre>
{''.join(cards)}
</html>"""
    p.write_text(content, encoding="utf-8")
