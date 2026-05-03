<?php
session_start();
require 'api.php';

$message = "";

// --- REGISTRIERUNG: Nutzer wird als stabiler DAD-Nutrient-Node angelegt ---
if (isset($_POST['register'])) {
    $response = call_mycelia('register_user', [
        'username' => $_POST['user'] ?? '',
        'password' => $_POST['pass'] ?? '',
        'profile' => [
            'vorname' => $_POST['vorname'] ?? '',
            'nachname' => $_POST['nachname'] ?? '',
            'strasse' => $_POST['strasse'] ?? '',
            'hnr' => $_POST['hnr'] ?? '',
            'plz' => $_POST['plz'] ?? '',
            'ort' => $_POST['ort'] ?? '',
            'email' => $_POST['email'] ?? ''
        ]
    ]);

    $message = ($response['status'] ?? '') === 'ok'
        ? "Registrierung erfolgreich: stabiler Mycelia-Attraktor erzeugt."
        : ($response['message'] ?? 'Registrierung fehlgeschlagen.');
}

// --- LOGIN: CognitiveCore prüft Username/Passwort-Hash gegen Attraktor ---
if (isset($_POST['login'])) {
    $response = call_mycelia('login_attractor', [
        'username' => $_POST['user'] ?? '',
        'password' => $_POST['pass'] ?? ''
    ]);

    if (($response['status'] ?? '') === 'ok') {
        $_SESSION['mycelia_signature'] = $response['signature'];
        $_SESSION['mycelia_username'] = $response['username'];
        header("Location: profile.php");
        exit;
    }
    $message = $response['message'] ?? "Falsche Daten.";
}

// Optionaler Dump-Import aus dem Frontend heraus.
if (isset($_POST['import_dump'])) {
    $response = call_mycelia('import_dump', [
        'path' => $_POST['dump_path'] ?? '',
        'table' => $_POST['dump_table'] ?? '',
        'limit' => ($_POST['dump_limit'] ?? '') === '' ? null : intval($_POST['dump_limit'])
    ]);
    $message = ($response['status'] ?? '') === 'ok'
        ? "Dump aufgenommen: " . intval($response['imported'] ?? 0) . " Nutrient-Nodes."
        : ($response['message'] ?? 'Import fehlgeschlagen.');
}
?>

<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>MyceliaDB Secure Login</title>
    <style>
        body { background: #121212; color: #00ff99; font-family: monospace; text-align: center; margin-top: 50px; }
        input { background: #333; border: 1px solid #555; color: white; padding: 5px; margin: 5px; width: 90%; box-sizing:border-box;}
        button { background: #00ff99; border: none; padding: 10px 20px; font-weight: bold; cursor: pointer; width: 100%; margin-top:10px;}
        .container { display: flex; justify-content: center; gap: 20px; flex-wrap: wrap; }
        .box { border: 1px solid #333; background: #1e1e1e; padding: 20px; width: 320px; text-align: left; }
        h2 { border-bottom: 1px solid #333; padding-bottom: 10px; margin-top: 0; }
        .message { color:#ffcc66; font-weight:bold; min-height: 1.2em; }
        .hint { color:#888; font-size: 12px; line-height: 1.4; }
    </style>
</head>
<body>
    <h1 style="border: 2px solid #00ff99; display:inline-block; padding: 10px;">MYCELIA SECURITY GATE</h1>
    <p class="message"><?= htmlspecialchars($message) ?></p>

    <div class="container">
        <div class="box">
            <h2>Login via Attractor</h2>
            <form method="post">
                <label>Username</label><br>
                <input type="text" name="user" required><br>
                <label>Passwort</label><br>
                <input type="password" name="pass" required><br>
                <button type="submit" name="login">ATTRAKTOR PRÜFEN</button>
            </form>
            <p class="hint">Keine users-Tabelle. Der Login wird als Muster gegen den CognitiveCore geprüft.</p>
        </div>

        <div class="box">
            <h2>Registrierung</h2>
            <form method="post">
                <label>Username</label><br>
                <input type="text" name="user" required><br>
                <label>Passwort</label><br>
                <input type="password" name="pass" required><br>
                <hr style="border-color:#333">
                <input type="text" name="vorname" placeholder="Vorname"><br>
                <input type="text" name="nachname" placeholder="Nachname"><br>
                <input type="text" name="strasse" placeholder="Straße"><br>
                <input type="text" name="hnr" placeholder="Nr." style="width: 25%"><input type="text" name="plz" placeholder="PLZ" style="width: 60%"><br>
                <input type="text" name="ort" placeholder="Ort"><br>
                <input type="email" name="email" placeholder="E-Mail"><br>
                <button type="submit" name="register">ALS NUTRIENT-NODE SPEICHERN</button>
            </form>
        </div>

        <div class="box">
            <h2>SQL-Dump fressen</h2>
            <form method="post">
                <label>Dump-Pfad auf dem Server</label><br>
                <input type="text" name="dump_path" placeholder="../Mycelia_Database-main/exports/customers.sql"><br>
                <label>Tabelle</label><br>
                <input type="text" name="dump_table" placeholder="customers"><br>
                <label>Limit optional</label><br>
                <input type="number" name="dump_limit" placeholder="100"><br>
                <button type="submit" name="import_dump">IMPORT ALS MYZEL-NETZ</button>
            </form>
            <p class="hint">Der Importer parst Dumps direkt und materialisiert keine SQL-Datenbank.</p>
        </div>
    </div>
</body>
</html>
