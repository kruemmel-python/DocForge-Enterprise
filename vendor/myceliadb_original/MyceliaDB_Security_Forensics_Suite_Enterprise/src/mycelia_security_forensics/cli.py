from __future__ import annotations

import argparse
from pathlib import Path
from datetime import datetime
import time
import sys
import mycelia_security_forensics

from . import __version__
from .config import load_config, overlay_cli
from .checks import ALL_CHECKS
from .findings import SuiteReport
from .report import summarize, write_json, write_markdown, write_html


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mycelia-forensics", description="Enterprise defensive forensics suite for MyceliaDB + SMQL Adapter + LM Studio.")
    p.add_argument("--config")
    p.add_argument("--adapter-url")
    p.add_argument("--mycelia-url")
    p.add_argument("--lmstudio-url")
    p.add_argument("--web-chat-api")
    p.add_argument("--token-file")
    p.add_argument("--adapter-root")
    p.add_argument("--mycelia-root")
    p.add_argument("--collection")
    p.add_argument("--chat-model")
    p.add_argument("--embedding-model")
    p.add_argument("--reports-dir", default=None)
    p.add_argument("--redteam-corpus")
    p.add_argument("--mycelia-pid", type=int)
    p.add_argument("--run-live-ram-probe", action="store_true")
    p.add_argument("--strict-exit-code", action="store_true")
    p.add_argument("--json-out")
    p.add_argument("--md-out")
    p.add_argument("--html-out")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = overlay_cli(load_config(args.config), args)
    reports_dir = Path(cfg.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.utcnow().isoformat() + "Z"
    t0 = time.time()
    findings = []
    print(f"MyceliaDB Enterprise Security Forensics Suite v{__version__}")
    print(f"Suite module: {Path(mycelia_security_forensics.__file__).resolve()}")
    print(f"Target collection: {cfg.collection}")
    for check in ALL_CHECKS:
        f = check(cfg)
        findings.append(f)
        icon = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "info": "INFO", "skip": "SKIP"}.get(f.status.value, f.status.value)
        print(f"[{icon}] {f.check_id} {f.title} — {f.summary}")
    duration = (time.time() - t0) * 1000
    summary = summarize(findings)
    report = SuiteReport(
        suite="MyceliaDB Enterprise Security Forensics Suite",
        version=__version__,
        target=cfg.to_public_dict(),
        started_at=started,
        duration_ms=duration,
        findings=findings,
        summary=summary,
    )
    json_out = args.json_out or str(reports_dir / "forensics_report.json")
    md_out = args.md_out or str(reports_dir / "forensics_report.md")
    html_out = args.html_out or str(reports_dir / "forensics_report.html")
    write_json(report, json_out)
    write_markdown(report, md_out)
    write_html(report, html_out)
    print(f"\nReports written:\n- {json_out}\n- {md_out}\n- {html_out}")
    if cfg.strict_exit_code and summary.get("enterprise_gate") == "fail":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
