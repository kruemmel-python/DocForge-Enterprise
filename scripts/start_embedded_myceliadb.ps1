$ErrorActionPreference = "Stop"
$port = if ($env:MYCELIA_PORT) { $env:MYCELIA_PORT } else { "9999" }
$root = if ($env:MYCELIA_ROOT) { $env:MYCELIA_ROOT } else { ".docforge_workspace/embedded_myceliadb" }
python -m myceliadb_embedded.cli --host 127.0.0.1 --port $port --root $root
