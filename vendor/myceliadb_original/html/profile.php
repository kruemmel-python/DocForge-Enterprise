<?php
session_start();
if (!isset($_SESSION['mycelia_signature'])) { header("Location: index.php"); exit; }
require 'api.php';

$signature = $_SESSION['mycelia_signature'];
$msg = "";

if (isset($_POST['logout'])) {
    session_destroy();
    header("Location: index.php");
    exit;
}

// --- UPDATE: Profil wird im DAD-Attraktor ersetzt und wieder GPU-verschlüsselt ---
if (isset($_POST['update'])) {
    $response = call_mycelia('update_profile', [
        'signature' => $signature,
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
    if (($response['status'] ?? '') === 'ok') {
        $signature = $response['signature'] ?? $signature;
        $_SESSION['mycelia_signature'] = $signature;
        $msg = "Profil im Myzel aktualisiert.";
    } else {
        $msg = $response['message'] ?? "Update fehlgeschlagen.";
    }
}

// --- DATEN LADEN: Rekonstruktion über QuantumOracle/Engine-Bridge ---
$response = require_mycelia_ok(call_mycelia('get_profile', ['signature' => $signature]));
$data = $response['profile'] ?? [];
$username = $response['username'] ?? ($_SESSION['mycelia_username'] ?? 'unknown');
$node = $response['node'] ?? [];
?>

<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>MyceliaDB Profile</title>
    <style>
        body { background: #121212; color: #e0e0e0; font-family: monospace; padding: 50px; }
        .box { background: #1e1e1e; border: 1px solid #333; padding: 20px; width: 560px; margin: 0 auto; }
        input { background: #222; border: 1px solid #444; color: white; padding: 8px; width: 100%; box-sizing: border-box; margin-bottom: 10px; }
        button { background: #00ff99; color: black; border: none; padding: 10px; font-weight: bold; cursor: pointer; width: 100%; }
        .raw { background: #000; color: #777; padding: 10px; border: 1px dashed #333; margin-bottom: 20px; font-size: 10px; word-break: break-all;}
        h1 { color: #00ff99; text-align: center; }
        .label { color: #888; font-size: 0.8em; }
        .logout { background:#333; color:#ddd; margin-top: 10px; }
    </style>
</head>
<body>
    <h1>Willkommen, <?= htmlspecialchars($username) ?></h1>

    <div class="box">
        <?php if($msg): ?><p style="color:#00ff99; text-align:center;"><?= htmlspecialchars($msg) ?></p><?php endif; ?>

        <h3>Mycelia-Nutrient-Node</h3>
        <div class="raw">
            SIGNATURE: <?= htmlspecialchars($signature) ?><br>
            STABILITY: <?= htmlspecialchars(strval($node['stability'] ?? 'n/a')) ?><br>
            TABLE: <?= htmlspecialchars(strval($node['table'] ?? 'mycelia_users')) ?><br>
            STORAGE: encrypted-attractor / no SQL
        </div>

        <h3>Rekonstruierte Profildaten</h3>
        <form method="post">
            <span class="label">Vorname</span><input type="text" name="vorname" value="<?= htmlspecialchars($data['vorname'] ?? '') ?>">
            <span class="label">Nachname</span><input type="text" name="nachname" value="<?= htmlspecialchars($data['nachname'] ?? '') ?>">
            <span class="label">Straße</span><input type="text" name="strasse" value="<?= htmlspecialchars($data['strasse'] ?? '') ?>">
            <div style="display:flex; gap:10px;">
                <div style="flex:1"><span class="label">Nr.</span><input type="text" name="hnr" value="<?= htmlspecialchars($data['hnr'] ?? '') ?>"></div>
                <div style="flex:2"><span class="label">PLZ</span><input type="text" name="plz" value="<?= htmlspecialchars($data['plz'] ?? '') ?>"></div>
            </div>
            <span class="label">Ort</span><input type="text" name="ort" value="<?= htmlspecialchars($data['ort'] ?? '') ?>">
            <span class="label">Email</span><input type="email" name="email" value="<?= htmlspecialchars($data['email'] ?? '') ?>">

            <button type="submit" name="update">UPDATE MYCELIA PROFILE</button>
            <button class="logout" type="submit" name="logout">Abmelden</button>
        </form>
    </div>
</body>
</html>
