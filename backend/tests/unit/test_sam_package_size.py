"""Unit tests verifying SAM packaging configuration and artifact size limits.

Conforms to spec 0002 AC 17.
Validates template structure, dependency exclusions, and Lambda package size bounds.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

COLLECTOR_DIR = Path(__file__).resolve().parents[2] / "src" / "functions" / "evidence_collector"
TEMPLATE_PATH = COLLECTOR_DIR / "template.yaml"
REQUIREMENTS_PATH = COLLECTOR_DIR / "requirements.txt"
BACKEND_SRC_DIR = Path(__file__).resolve().parents[2] / "src"

MAX_COMPRESSED_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024  # 250 MB

FORBIDDEN_HEAVY_PACKAGES = {
    "langgraph",
    "langchain",
    "playwright",
    "ortools",
    "geopandas",
    "shapely",
    "reportlab",
    "fastapi",
    "uvicorn",
}

REQUIRED_LEAN_PACKAGES = {
    "pydantic",
    "sqlalchemy",
    "asyncpg",
    "httpx",
    "scrapling",
}


def _load_cfn_template(path: Path) -> dict[str, Any]:
    """Helper loading CloudFormation SAM YAML with custom tag constructors."""
    class CfnSafeLoader(yaml.SafeLoader):  # type: ignore[misc]
        pass

    def cfn_constructor(loader: Any, node: Any) -> Any:
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        return None

    for tag in ["!Ref", "!Sub", "!GetAtt", "!Join", "!Select", "!Split", "!FindInMap"]:
        CfnSafeLoader.add_constructor(tag, cfn_constructor)

    with open(path, encoding="utf-8") as f:
        return cast(dict[str, Any], yaml.load(f, Loader=CfnSafeLoader))




def test_template_existence_and_validity() -> None:
    """Verifies that the SAM template exists and contains valid serverless definitions."""
    assert TEMPLATE_PATH.exists(), f"SAM template missing at {TEMPLATE_PATH}"

    template = _load_cfn_template(TEMPLATE_PATH)

    assert template.get("AWSTemplateFormatVersion") == "2010-09-09"
    assert template.get("Transform") == "AWS::Serverless-2016-10-31"

    resources = template.get("Resources", {})
    assert "EvidenceCollectorFunction" in resources
    assert "EvidenceCollectorQueue" in resources
    assert "EvidenceCollectorDLQ" in resources

    # Function configuration
    fn_props = resources["EvidenceCollectorFunction"].get("Properties", {})
    assert fn_props.get("Handler") == "functions.evidence_collector.consumer.lambda_handler"
    assert fn_props.get("Runtime") == "python3.12"
    assert fn_props.get("MemorySize") == 512

    # Queue configuration with DLQ redrive
    queue_props = resources["EvidenceCollectorQueue"].get("Properties", {})
    redrive = queue_props.get("RedrivePolicy", {})
    assert redrive.get("maxReceiveCount") == 5


def test_requirements_lean_dependencies() -> None:
    """Verifies that requirements.txt includes lean packages and excludes heavy ones."""
    assert REQUIREMENTS_PATH.exists(), f"requirements.txt missing at {REQUIREMENTS_PATH}"

    lines: list[str] = []
    with open(REQUIREMENTS_PATH, encoding="utf-8") as f:
        for raw_line in f:
            cleaned = raw_line.strip()
            if cleaned and not cleaned.startswith("#"):
                pkg_name = cleaned.split(">=")[0].split("==")[0].split("[")[0].strip().lower()
                lines.append(pkg_name)

    present_packages = set(lines)

    # Required packages must be present
    for req in REQUIRED_LEAN_PACKAGES:
        assert req in present_packages, f"Required dependency {req} missing from requirements.txt"

    # Forbidden heavy packages must be excluded
    for forbidden in FORBIDDEN_HEAVY_PACKAGES:
        assert forbidden not in present_packages, (
            f"Heavy package {forbidden} must be excluded from evidence collector requirements.txt"
        )


def test_staged_source_package_size_within_limits() -> None:
    """AC 17: Verifies that staged collector source and ZIP package strictly satisfy size limits."""
    # Build in memory zip archive of staged source files
    zip_buffer = io.BytesIO()
    total_uncompressed_bytes = 0

    staged_subpaths = [
        "functions/evidence_collector",
        "config",
        "contracts",
        "functions/orchestrator/execution/fencing.py",
        "functions/orchestrator/execution/models.py",
        "functions/orchestrator/execution/job_events.py",
    ]

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for subpath in staged_subpaths:
            full_path = BACKEND_SRC_DIR / subpath
            if full_path.is_file():
                file_bytes = full_path.read_bytes()
                zf.writestr(subpath, file_bytes)
                total_uncompressed_bytes += len(file_bytes)
            elif full_path.is_dir():
                for root, _, files in os.walk(full_path):
                    if "__pycache__" in root:
                        continue
                    for f in files:
                        if f.endswith(".pyc"):
                            continue
                        f_path = Path(root) / f
                        rel_archive_path = str(f_path.relative_to(BACKEND_SRC_DIR))
                        file_bytes = f_path.read_bytes()
                        zf.writestr(rel_archive_path, file_bytes)
                        total_uncompressed_bytes += len(file_bytes)

    compressed_bytes = zip_buffer.tell()

    # AC 17 size assertions
    assert total_uncompressed_bytes < MAX_UNCOMPRESSED_BYTES, (
        f"Uncompressed size {total_uncompressed_bytes} exceeds limit of {MAX_UNCOMPRESSED_BYTES} bytes"
    )
    assert compressed_bytes < MAX_COMPRESSED_BYTES, (
        f"Compressed size {compressed_bytes} exceeds limit of {MAX_COMPRESSED_BYTES} bytes"
    )

    # Uncompressed source should be lean (under 10 MB for staged source files)
    assert total_uncompressed_bytes < 10 * 1024 * 1024
    # Compressed archive should be under 5 MB for source files
    assert compressed_bytes < 5 * 1024 * 1024


def test_sam_build_artifact_inspection() -> None:
    """Verifies build artifact directory generated during sam build."""
    build_dir = Path(__file__).resolve().parents[3] / ".aws-sam" / "build" / "EvidenceCollectorFunction"
    if not build_dir.exists():
        pytest.skip("sam build artifact not generated yet; run sam build first")

    # Check that collector entrypoint exists in built artifact
    entrypoint = build_dir / "functions" / "evidence_collector" / "consumer.py"
    assert entrypoint.exists(), f"Entrypoint missing in built artifact: {entrypoint}"

    # Calculate actual size of the built artifact directory
    total_size = sum(f.stat().st_size for f in build_dir.rglob("*") if f.is_file())
    assert total_size < MAX_UNCOMPRESSED_BYTES, (
        f"SAM built artifact size {total_size} exceeds {MAX_UNCOMPRESSED_BYTES} bytes"
    )
