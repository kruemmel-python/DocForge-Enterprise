<?php
/**
 * Mycelia Frontend API Bridge
 *
 * Spricht ausschließlich mit der lokalen Mycelia-Plattform-Engine
 * (mycelia_platform.py). Es gibt keine PDO-/MySQL-Verbindung mehr.
 */
function call_mycelia($command, $payload = []) {
    $url = getenv('MYCELIA_API_URL') ?: 'http://127.0.0.1:9999';

    $data = [
        'command' => $command,
        'action' => $command, // Kompatibilität mit älteren Proxies
        'payload' => $payload
    ];

    $options = [
        'http' => [
            'header' => "Content-type: application/json\r\n",
            'method' => 'POST',
            'timeout' => 20,
            'content' => json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES)
        ]
    ];
    $context = stream_context_create($options);
    $result = @file_get_contents($url, false, $context);

    if ($result === false) {
        http_response_code(503);
        die("<div style='background:#7a0000;color:white;padding:20px;font-family:monospace;'>" .
            "FEHLER: Die autarke Mycelia-Engine läuft nicht.<br>" .
            "Starte im html-Verzeichnis: <b>python mycelia_platform.py</b>" .
            "</div>");
    }

    $decoded = json_decode($result, true);
    if (!is_array($decoded)) {
        return ['status' => 'error', 'message' => 'Ungültige Antwort der Mycelia-Engine'];
    }
    return $decoded;
}

function require_mycelia_ok($response) {
    if (!is_array($response) || ($response['status'] ?? 'error') !== 'ok') {
        $msg = htmlspecialchars($response['message'] ?? 'Unbekannter Mycelia-Fehler');
        die("<h1 style='color:red;font-family:monospace'>MYCELIA ERROR</h1><pre>$msg</pre>");
    }
    return $response;
}
?>
