<?php
http_response_code(410);
?>
<!doctype html>
<html lang="de">
<head><meta charset="utf-8"><title>MyceliaDB Setup</title></head>
<body style="background:#121212;color:#00ff99;font-family:monospace;padding:40px">
<h1>Relationales Setup entfernt</h1>
<p>Diese Plattform erzeugt keine klassische Datenbank und keine users-Tabelle mehr.</p>
<p>Starte stattdessen im html-Verzeichnis:</p>
<pre>python mycelia_platform.py</pre>
<p>Danach verwendet index.php ausschließlich die Mycelia-Engine-API.</p>
</body>
</html>
