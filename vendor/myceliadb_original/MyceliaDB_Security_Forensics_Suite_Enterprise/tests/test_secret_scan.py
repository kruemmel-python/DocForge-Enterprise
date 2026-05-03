import unittest
import tempfile
from pathlib import Path
from mycelia_security_forensics.secret_scan import scan_paths


class SecretScanTests(unittest.TestCase):
    def test_detects_literal_secret(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.txt"
            p.write_text("token=supersecretvalue123", encoding="utf-8")
            hits = scan_paths([d], [".txt"], literal_secret="supersecretvalue123")
            self.assertTrue(hits)

    def test_ignores_virtualenv_site_packages(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".venv" / "Lib" / "site-packages" / "pip" / "auth.py"
            p.parent.mkdir(parents=True)
            p.write_text("password = notarealsecret123456789\n", encoding="utf-8")
            hits = scan_paths([d], [".py"])
            self.assertEqual(hits, [])

    def test_ignores_variable_assignment_in_source_code(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "adapter.py"
            p.write_text("settings.mycelia.token = args.mycelia_token\n", encoding="utf-8")
            hits = scan_paths([d], [".py"])
            self.assertEqual(hits, [])


    def test_ignores_token_loaded_from_function_call(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.py"
            p.write_text(
                "token = _read_token_file(explicit)\n"
                "token = _read_token_file(candidate)\n",
                encoding="utf-8",
            )
            hits = scan_paths([d], [".py"])
            self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
