"""Unit tests verifying standalone SAM packaging, isolated artifact boundaries, and Lambda size limits.

Conforms to spec 0002 AC 17.
Validates:
1. SAM template validity, CodeUri alignment, and environment variables matching application Settings.
2. Lean dependency boundaries in requirements.txt (excluding heavy libraries like OR-Tools, LangGraph, Playwright).
3. Dependency-inclusive artifact building with strict module isolation and size measurement against AWS limits:
   - Compressed ZIP package <= 50 MB
   - Unpacked artifact <= 250 MB
4. Lambda-compatible clean environment handler import proof.
5. SAM build artifact verification.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from functions.evidence_collector.packaging import (
    FORBIDDEN_APP_MODULES,
    FORBIDDEN_HEAVY_PACKAGES,
    MAX_COMPRESSED_BYTES,
    MAX_UNCOMPRESSED_BYTES,
    build_isolated_collector_artifact,
    resolve_python_runtime_binary,
    verify_handler_in_clean_environment,
)

COLLECTOR_DIR = Path(__file__).resolve().parents[2] / "src" / "functions" / "evidence_collector"
TEMPLATE_PATH = COLLECTOR_DIR / "template.yaml"
REQUIREMENTS_PATH = COLLECTOR_DIR / "requirements.txt"
BACKEND_DIR = Path(__file__).resolve().parents[2]

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


def test_template_existence_validity_and_settings_alignment() -> None:
    """Verifies that the SAM template exists, is valid, and aligns environment values with Settings."""
    assert TEMPLATE_PATH.exists(), f"SAM template missing at {TEMPLATE_PATH}"

    template = _load_cfn_template(TEMPLATE_PATH)

    assert template.get("AWSTemplateFormatVersion") == "2010-09-09"
    assert template.get("Transform") == "AWS::Serverless-2016-10-31"

    # Verify Environment parameter aligns with Settings.ENVIRONMENT literal values
    parameters = template.get("Parameters", {})
    env_param = parameters.get("Environment", {})
    assert env_param.get("AllowedValues") == ["local", "staging", "production"]
    assert env_param.get("Default") in ["local", "staging", "production"]

    # Verify Globals Function environment variables align with Settings
    globals_sec = template.get("Globals", {})
    fn_globals = globals_sec.get("Function", {})
    env_vars = fn_globals.get("Environment", {}).get("Variables", {})
    assert "ENVIRONMENT" in env_vars
    assert "DATABASE_URL" in env_vars
    assert "SQS_QUEUE_URL" in env_vars
    assert "SERPAPI_API_KEY" in env_vars
    assert "SERPAPI_BASE_URL" in env_vars
    assert "HTTP_REQUEST_TIMEOUT_SECONDS" in env_vars
    assert "MAX_SOURCE_PAYLOAD_BYTES" in env_vars

    resources = template.get("Resources", {})
    assert "EvidenceCollectorFunction" in resources
    assert "EvidenceCollectorQueue" in resources
    assert "EvidenceCollectorDLQ" in resources

    # Function configuration
    fn_props = resources["EvidenceCollectorFunction"].get("Properties", {})
    assert fn_props.get("Handler") == "functions.evidence_collector.consumer.lambda_handler"
    assert fn_props.get("Runtime") == "python3.12"
    assert fn_props.get("Architectures") == ["x86_64"]
    assert fn_props.get("MemorySize") == 512
    assert fn_props.get("CodeUri") == "../../../build/evidence_collector"

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


def test_dependency_inclusive_package_size_and_isolated_artifact() -> None:
    """AC 17: Verifies that the actual dependency-inclusive artifact and ZIP archive satisfy Lambda size limits."""
    output_dir, zip_path, uncompressed_bytes, compressed_bytes = build_isolated_collector_artifact()

    assert output_dir.exists()
    assert zip_path.exists()

    # Verify presence of installed dependencies
    assert (output_dir / "pydantic").exists()
    assert (output_dir / "sqlalchemy").exists()
    assert (output_dir / "asyncpg").exists()
    assert (output_dir / "httpx").exists()
    assert (output_dir / "scrapling").exists()

    # Verify presence of permitted collector and shared execution files
    assert (output_dir / "functions" / "evidence_collector" / "consumer.py").exists()
    assert (output_dir / "functions" / "evidence_collector" / "service.py").exists()
    assert (output_dir / "functions" / "orchestrator" / "execution" / "fencing.py").exists()
    assert (output_dir / "functions" / "orchestrator" / "execution" / "models.py").exists()
    assert (output_dir / "config" / "config.py").exists()
    assert (output_dir / "contracts" / "evidence.py").exists()
    assert (output_dir / "contracts" / "money.py").exists()

    # Strict isolation: forbidden application modules must be excluded
    for mod_path in FORBIDDEN_APP_MODULES:
        rel_parts = mod_path.split(".")
        target_path = output_dir.joinpath(*rel_parts)
        assert not target_path.exists(), f"Forbidden app module {mod_path} found in artifact: {target_path}"

    # Strict isolation: forbidden heavy third-party packages must be excluded
    for pkg in FORBIDDEN_HEAVY_PACKAGES:
        pkg_dir = output_dir / pkg
        assert not pkg_dir.exists(), f"Forbidden heavy package {pkg} found in artifact: {pkg_dir}"

    # AC 17 size assertions on the actual dependency-inclusive package
    assert uncompressed_bytes < MAX_UNCOMPRESSED_BYTES, (
        f"Dependency-inclusive uncompressed size {uncompressed_bytes} bytes exceeds {MAX_UNCOMPRESSED_BYTES} bytes"
    )
    assert compressed_bytes < MAX_COMPRESSED_BYTES, (
        f"Dependency-inclusive compressed ZIP size {compressed_bytes} bytes exceeds {MAX_COMPRESSED_BYTES} bytes"
    )


def test_verify_handler_import_clean_lambda_environment() -> None:
    """Verifies that the consumer handler imports cleanly in a Lambda-compatible environment with only artifact on PYTHONPATH."""
    output_dir = BACKEND_DIR / "build" / "evidence_collector"
    if not output_dir.exists():
        output_dir, _, _, _ = build_isolated_collector_artifact()

    result = verify_handler_in_clean_environment(output_dir)
    assert result["status"] == "SUCCESS"


def test_sam_build_artifact_inspection() -> None:
    """Verifies build artifact directory generated during sam build."""
    build_dir = BACKEND_DIR / ".aws-sam" / "build" / "EvidenceCollectorFunction"
    if not build_dir.exists():
        pytest.skip("sam build artifact not generated yet; run sam build first")

    # Check that collector entrypoint exists in built artifact
    entrypoint = build_dir / "functions" / "evidence_collector" / "consumer.py"
    assert entrypoint.exists(), f"Entrypoint missing in built artifact: {entrypoint}"

    # Forbidden modules must not exist in built SAM artifact
    assert not (build_dir / "functions" / "optimizer").exists()
    assert not (build_dir / "functions" / "api").exists()
    assert not (build_dir / "functions" / "orchestrator" / "evidence").exists()
    assert not (build_dir / "functions" / "orchestrator" / "places").exists()
    assert not (build_dir / "functions" / "orchestrator" / "graphs").exists()

    # Calculate actual size of the built artifact directory
    total_size = sum(f.stat().st_size for f in build_dir.rglob("*") if f.is_file())
    assert total_size < MAX_UNCOMPRESSED_BYTES, (
        f"SAM built artifact size {total_size} exceeds {MAX_UNCOMPRESSED_BYTES} bytes"
    )


def test_cached_artifact_build_avoids_reinstallation() -> None:
    """Verifies that subsequent builds reuse cached dependencies when requirements.txt has not changed."""
    import time

    start_time = time.perf_counter()
    output_dir, zip_path, uncompressed_bytes, compressed_bytes = build_isolated_collector_artifact(
        force_reinstall=False
    )
    elapsed = time.perf_counter() - start_time

    assert output_dir.exists()
    assert (output_dir / ".requirements.hash").exists()
    assert (output_dir / "pydantic").exists()
    assert (output_dir / "contracts" / "money.py").exists()
    assert uncompressed_bytes < MAX_UNCOMPRESSED_BYTES
    # Cached run should complete in under 5 seconds (typically ~0.5s without pip)
    assert elapsed < 5.0, f"Cached build took too long ({elapsed:.2f}s); dependency caching failed"


def test_resolve_python_runtime_binary_fails_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 17: Verifies that runtime resolution fails explicitly when Python 3.12 is unavailable."""
    from unittest.mock import MagicMock

    # Simulate missing uv managed paths and failed which
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    mock_run = MagicMock(return_value=type("SubResult", (), {"returncode": 1, "stdout": ""})())
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    monkeypatch.setattr("sys.version_info", (3, 13, 0, "final", 0))

    with pytest.raises(RuntimeError, match="Required Python 3.12 runtime is unavailable"):
        resolve_python_runtime_binary()


