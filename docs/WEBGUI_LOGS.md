# WebGUI Log Streaming

The secure WebGUI keeps an in-memory log buffer per job and polls `/api/job/<id>`
from the browser. Logs are shown immediately after pressing **Dokumentation erstellen**.

This release hardens the log display by:

- writing an immediate WebGUI-side start line before the subprocess emits output
- logging input path, workspace and the generated CLI command
- avoiding browser-global DOM variables such as `log`, `jobs` or `current`
- auto-scrolling the log panel
- showing polling errors in the log panel instead of silently stopping
