<?php
declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');
header('X-Content-Type-Options: nosniff');
$reportPath = realpath(__DIR__ . '/../reports/forensics_report.json');
if (!$reportPath || !file_exists($reportPath)) {
    http_response_code(404);
    echo json_encode(['status' => 'error', 'message' => 'report-not-found'], JSON_UNESCAPED_UNICODE);
    exit;
}
$raw = file_get_contents($reportPath);
$data = json_decode($raw, true);
if (!is_array($data)) {
    http_response_code(500);
    echo json_encode(['status' => 'error', 'message' => 'report-invalid-json'], JSON_UNESCAPED_UNICODE);
    exit;
}
echo json_encode(['status' => 'ok', 'report' => $data], JSON_UNESCAPED_UNICODE);
