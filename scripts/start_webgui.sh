#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${MYCELIA_LOCAL_TOKEN:-}" ]]; then
  export MYCELIA_LOCAL_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
  echo "Generated temporary MYCELIA_LOCAL_TOKEN for this shell."
fi

docforge-webgui --host 127.0.0.1 --port 7860
