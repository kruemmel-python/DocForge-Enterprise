import unittest
import tempfile
from pathlib import Path
import json
from mycelia_security_forensics.rehydration import audit_jsonl


class RehydrationAuditTests(unittest.TestCase):
    def test_latest_record_wins(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            p.write_text(
                json.dumps({"collection":"demo","id":"a"}) + "\n" +
                json.dumps({"collection":"demo","id":"b"}) + "\n" +
                json.dumps({"collection":"demo","id":"a","deleted":True}) + "\n",
                encoding="utf-8"
            )
            audit = audit_jsonl(str(p))
            self.assertTrue(audit.exists)
            self.assertEqual(audit.latest_counts["demo"], 1)
            self.assertEqual(audit.events_total, 3)
            self.assertEqual(audit.events_failed, 0)


if __name__ == "__main__":
    unittest.main()
