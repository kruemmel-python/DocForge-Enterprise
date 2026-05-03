import unittest
from mycelia_security_forensics.findings import Finding, Status, Severity
from mycelia_security_forensics.report import summarize


class ReportModelTests(unittest.TestCase):
    def test_summary_gate_pass(self):
        s = summarize([Finding("A", "A", Status.PASS, Severity.INFO)])
        self.assertEqual(s["enterprise_gate"], "pass")

    def test_summary_gate_fail(self):
        s = summarize([Finding("A", "A", Status.FAIL, Severity.HIGH)])
        self.assertEqual(s["enterprise_gate"], "fail")

    def test_summary_gate_warn(self):
        s = summarize([Finding("A", "A", Status.WARN, Severity.MEDIUM)])
        self.assertEqual(s["enterprise_gate"], "warn")


if __name__ == "__main__":
    unittest.main()
