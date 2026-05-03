# WebGUI Session and Language Boot Fix

This release fixes a browser-side boot loop that could leave the WebGUI stuck at
"Session wird geprüft ..." / "Checking session ...".

Changes:

- unauthenticated clients no longer poll protected endpoints
- polling starts only after `/api/me` confirms an authenticated session
- switching or initializing the German UI no longer triggers an automatic reload loop
- client disconnects during reload/polling are ignored server-side instead of printing tracebacks
- session checks fail closed and show the login/registration panel

Recommended troubleshooting:

1. Stop the WebGUI server.
2. Clear site data for `127.0.0.1:7860` or open an incognito/private window.
3. Restart on a fresh port.
4. Register or log in again.
