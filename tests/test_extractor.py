import zipfile
from pathlib import Path

import pytest

from docforge_enterprise.config import Settings
from docforge_enterprise.extractor import prepare_input


def test_zip_slip_is_rejected(tmp_path: Path) -> None:
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../evil.txt", "nope")

    settings = Settings()
    settings.pipeline.workspace = tmp_path / "workspace"

    with pytest.raises(ValueError):
        prepare_input(zip_path, settings.pipeline.workspace, settings)
