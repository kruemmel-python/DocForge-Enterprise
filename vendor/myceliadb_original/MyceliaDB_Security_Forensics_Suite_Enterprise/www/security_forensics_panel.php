<?php
declare(strict_types=1);
$reportPath = realpath(__DIR__ . '/../reports/forensics_report.json');
$report = null;
$error = null;
if ($reportPath && file_exists($reportPath)) {
    $raw = file_get_contents($reportPath);
    $report = json_decode($raw, true);
    if (!is_array($report)) {
        $error = 'Report exists but is not valid JSON.';
    }
} else {
    $error = 'No report found. Run the suite first: .\\run_all.ps1';
}
function h($s) { return htmlspecialchars((string)$s, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8'); }
?>
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>MyceliaDB Security Forensics</title>
  <link rel="stylesheet" href="assets/security-forensics.css">
</head>
<body>
<main class="shell">
  <header>
    <h1>MyceliaDB Security Forensics</h1>
    <p>Read-only Enterprise Security Panel für lokale Reports.</p>
  </header>
  <?php if ($error): ?>
    <section class="card warn"><h2>Kein Report</h2><p><?=h($error)?></p></section>
  <?php else: ?>
    <section class="grid">
      <div class="card"><b>Suite</b><span><?=h($report['suite'] ?? '')?></span></div>
      <div class="card"><b>Version</b><span><?=h($report['version'] ?? '')?></span></div>
      <div class="card"><b>Gate</b><span class="gate <?=h($report['summary']['enterprise_gate'] ?? '')?>"><?=h($report['summary']['enterprise_gate'] ?? '')?></span></div>
      <div class="card"><b>Findings</b><span><?=h($report['summary']['total'] ?? 0)?></span></div>
    </section>
    <section class="card">
      <h2>Summary</h2>
      <pre><?=h(json_encode($report['summary'] ?? [], JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE))?></pre>
    </section>
    <?php foreach (($report['findings'] ?? []) as $f): ?>
      <section class="card finding <?=h($f['status'] ?? '')?>">
        <h2><?=h($f['check_id'] ?? '')?> — <?=h($f['title'] ?? '')?></h2>
        <p><b>Status:</b> <?=h($f['status'] ?? '')?> · <b>Severity:</b> <?=h($f['severity'] ?? '')?> · <b>Category:</b> <?=h($f['category'] ?? '')?></p>
        <p><?=h($f['summary'] ?? '')?></p>
        <details><summary>Evidence</summary><pre><?=h(json_encode($f['evidence'] ?? [], JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE))?></pre></details>
      </section>
    <?php endforeach; ?>
  <?php endif; ?>
</main>
</body>
</html>
