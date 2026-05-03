import unittest
from dataclasses import asdict, is_dataclass
from mycelia_security_forensics.secret_scan import SecretHit

class SecretHitSerializationTests(unittest.TestCase):
    def test_slots_dataclass_uses_asdict_not_dict(self):
        h = SecretHit("x.txt", 1, "token", "token=<redacted>")
        self.assertFalse(hasattr(h, "__dict__"))
        self.assertTrue(is_dataclass(h))
        self.assertEqual(asdict(h)["path"], "x.txt")

if __name__ == "__main__":
    unittest.main()
