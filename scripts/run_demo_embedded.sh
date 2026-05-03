#!/usr/bin/env bash
set -euo pipefail
python -m docforge_enterprise.cli examples/sample_project.zip --embedded-mycelia --dry-run --force-rebuild --json
