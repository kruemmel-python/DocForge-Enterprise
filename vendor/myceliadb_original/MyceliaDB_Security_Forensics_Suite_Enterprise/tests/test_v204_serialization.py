import unittest
from dataclasses import dataclass
from mycelia_security_forensics.findings import Finding, Status, Severity

@dataclass(slots=True)
class Slotted:
    path: str
    line: int

class V204SerializationTests(unittest.TestCase):
    def test_slotted_dataclass_evidence_serializes(self):
        f = Finding("X", "Y", Status.PASS, Severity.INFO, evidence={"hit": Slotted("a", 1)})
        d = f.to_dict()
        self.assertEqual(d["evidence"]["hit"]["path"], "a")

if __name__ == "__main__":
    unittest.main()
