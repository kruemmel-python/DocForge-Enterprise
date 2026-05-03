from pathlib import Path

from docforge_enterprise.config import SecuritySettings
from docforge_enterprise.security import redact_secrets, should_skip_path


def test_secret_path_is_skipped() -> None:
    decision = should_skip_path(Path("html/keys/local_transport.token"), SecuritySettings())
    assert decision.allowed


def test_vendor_path_is_skipped() -> None:
    decision = should_skip_path(Path("project/.venv/Lib/site.py"), SecuritySettings())
    assert decision.allowed


def test_secret_redaction() -> None:
    text, count = redact_secrets("api_key = 'abcdefghijklmnopqrstuvwxyz123456'")
    assert count >= 1
    assert "REDACTED_SECRET" in text
