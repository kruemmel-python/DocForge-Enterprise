#!/usr/bin/env bash
set -euo pipefail
python -m myceliadb_embedded.cli --host 127.0.0.1 --port "${MYCELIA_PORT:-9999}" --root "${MYCELIA_ROOT:-.docforge_workspace/embedded_myceliadb}"
