import zipfile
from pathlib import Path

from docforge_enterprise.config import Settings
from docforge_enterprise.pipeline import DocumentationPipeline


def test_pipeline_dry_run(tmp_path: Path) -> None:
    project_zip = tmp_path / "sample.zip"
    with zipfile.ZipFile(project_zip, "w") as zf:
        zf.writestr("src/app.py", "def hello():\n    return 'world'\n")
        zf.writestr("README.md", "# Sample\n")

    settings = Settings()
    settings.pipeline.workspace = tmp_path / "workspace"
    settings.pipeline.dry_run = True
    settings.pipeline.force_rebuild = True

    pipeline = DocumentationPipeline(input_path=project_zip, settings=settings)
    try:
        result = pipeline.run()
    finally:
        pipeline.close()

    assert Path(result.output_paths["markdown"]).exists()
    assert result.metadata["stats"]["files_indexed"] >= 1
