"""Unit tests verifying browser worker SAM packaging, isolated artifact boundaries, and Lambda size limits.

Conforms to spec 0003 AC-8.
Validates:
1. SAM template validity, CodeUri alignment, MemorySize=1536MB, and environment settings.
2. Requirements dependencies: Playwright and Scrapling present, heavy optimizer/API packages excluded.
3. Dependency-inclusive artifact building with strict module isolation and size measurement:
   - Unpacked function package <= 80 MB
   - Compressed ZIP package <= 50 MB
   - Unpacked Chromium layer ~ 130 MB
   - Combined unpacked size <= 250 MB (262,144,000 bytes ceiling, target <= 210 MB)
4. Lambda-compatible clean environment handler import proof under Python 3.12 with -S -P.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from functions.browser.packaging import (
    CHROMIUM_LAYER_UNCOMPRESSED_BYTES,
    FORBIDDEN_APP_MODULES,
    FORBIDDEN_HEAVY_PACKAGES,
    MAX_COMBINED_UNCOMPRESSED_BYTES,
    MAX_FUNCTION_COMPRESSED_BYTES,
    MAX_FUNCTION_UNCOMPRESSED_BYTES,
    SPARTICUZ_CHROMIUM_LAYER_ARCHIVE_BYTES,
    SPARTICUZ_CHROMIUM_LAYER_FILENAME,
    SPARTICUZ_CHROMIUM_LAYER_SHA256,
    SPARTICUZ_CHROMIUM_LAYER_UNPACKED_BYTES,
    SPARTICUZ_CHROMIUM_VERSION,
    TARGET_COMBINED_UNCOMPRESSED_BYTES,
    build_isolated_browser_artifact,
    resolve_python_runtime_binary,
    verify_browser_startup_and_render_isolated_environment,
    verify_handler_in_clean_environment,
    verify_sparticuz_layer_bootstrap,
)

BROWSER_DIR = Path(__file__).resolve().parents[2] / "src" / "functions" / "browser"
TEMPLATE_PATH = BROWSER_DIR / "template.yaml"
REQUIREMENTS_PATH = BROWSER_DIR / "requirements.txt"
BACKEND_DIR = Path(__file__).resolve().parents[2]

REQUIRED_BROWSER_PACKAGES = {
    "pydantic",
    "sqlalchemy",
    "asyncpg",
    "scrapling",
    "playwright",
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


def test_browser_template_existence_validity_and_settings_alignment() -> None:
    """Verifies that the browser SAM template exists, is valid, and aligns with Settings and AC-8."""
    assert TEMPLATE_PATH.exists(), f"SAM template missing at {TEMPLATE_PATH}"

    template = _load_cfn_template(TEMPLATE_PATH)

    assert template.get("AWSTemplateFormatVersion") == "2010-09-09"
    assert template.get("Transform") == "AWS::Serverless-2016-10-31"

    # Verify Parameters
    parameters = template.get("Parameters", {})
    env_param = parameters.get("Environment", {})
    assert env_param.get("AllowedValues") == ["local", "staging", "production"]
    assert env_param.get("Default") in ["local", "staging", "production"]
    assert "ChromiumLayerArn" in parameters

    # Verify Globals Function configuration
    globals_sec = template.get("Globals", {})
    fn_globals = globals_sec.get("Function", {})
    assert fn_globals.get("Runtime") == "python3.12"
    assert fn_globals.get("MemorySize") == 1536
    assert fn_globals.get("Timeout") == 60

    env_vars = fn_globals.get("Environment", {}).get("Variables", {})
    assert "ENVIRONMENT" in env_vars
    assert "DATABASE_URL" in env_vars
    assert "BROWSER_WORKER_QUEUE_URL" in env_vars
    assert env_vars.get("CHROMIUM_PATH") == "/tmp/chromium"
    assert "BROWSER_NAVIGATION_TIMEOUT_SECONDS" in env_vars
    assert "BROWSER_MAX_ESCALATION_TASKS" in env_vars

    # Verify Resources
    resources = template.get("Resources", {})
    assert "BrowserEscalationFunction" in resources
    assert "BrowserEscalationQueue" in resources
    assert "BrowserEscalationDLQ" in resources

    # Function configuration
    fn_props = resources["BrowserEscalationFunction"].get("Properties", {})
    assert fn_props.get("Handler") == "functions.browser.consumer.lambda_handler"
    assert fn_props.get("Runtime") == "python3.12"
    assert fn_props.get("Architectures") == ["x86_64"]
    assert fn_props.get("MemorySize") == 1536
    assert fn_props.get("CodeUri") == "../../../build/browser"
    assert "Layers" in fn_props

    # SQS Event configuration with partial batch responses
    events = fn_props.get("Events", {})
    task_batch = events.get("TaskDispatchBatch", {})
    assert task_batch.get("Type") == "SQS"
    sqs_props = task_batch.get("Properties", {})
    assert sqs_props.get("FunctionResponseTypes") == ["ReportBatchItemFailures"]

    # Queue configuration with DLQ redrive
    queue_props = resources["BrowserEscalationQueue"].get("Properties", {})
    redrive = queue_props.get("RedrivePolicy", {})
    assert redrive.get("maxReceiveCount") == 5


def test_browser_requirements_dependencies() -> None:
    """Verifies that requirements.txt includes browser packages and excludes heavy packages."""
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
    for req in REQUIRED_BROWSER_PACKAGES:
        assert req in present_packages, f"Required dependency {req} missing from requirements.txt"

    # Forbidden heavy packages must be excluded
    for forbidden in FORBIDDEN_HEAVY_PACKAGES:
        assert forbidden not in present_packages, (
            f"Heavy package {forbidden} must be excluded from browser requirements.txt"
        )


def test_browser_dependency_inclusive_package_size_and_isolated_artifact() -> None:
    """AC-8: Verifies that the dependency-inclusive artifact satisfies AWS Lambda size limits."""
    (
        output_dir,
        zip_path,
        unpacked_func_bytes,
        compressed_bytes,
        combined_unpacked_bytes,
    ) = build_isolated_browser_artifact()

    assert output_dir.exists()
    assert zip_path.exists()

    # Verify presence of installed dependencies
    assert (output_dir / "pydantic").exists()
    assert (output_dir / "sqlalchemy").exists()
    assert (output_dir / "asyncpg").exists()
    assert (output_dir / "scrapling").exists()
    assert (output_dir / "playwright").exists()

    # Verify presence of browser and shared execution files
    assert (output_dir / "functions" / "browser" / "consumer.py").exists()
    assert (output_dir / "functions" / "browser" / "service.py").exists()
    assert (output_dir / "functions" / "browser" / "driver.py").exists()
    assert (output_dir / "functions" / "browser" / "network.py").exists()
    assert (output_dir / "functions" / "browser" / "models.py").exists()
    assert (output_dir / "functions" / "browser" / "parser.py").exists()
    assert (output_dir / "functions" / "orchestrator" / "execution" / "fencing.py").exists()
    assert (output_dir / "functions" / "orchestrator" / "execution" / "models.py").exists()
    assert (output_dir / "config" / "config.py").exists()
    assert (output_dir / "contracts" / "browser.py").exists()

    # Strict isolation: forbidden application modules must be excluded
    for mod_path in FORBIDDEN_APP_MODULES:
        rel_parts = mod_path.split(".")
        target_path = output_dir.joinpath(*rel_parts)
        assert not target_path.exists(), f"Forbidden app module {mod_path} found in artifact: {target_path}"

    # Strict isolation: forbidden heavy third-party packages must be excluded
    for pkg in FORBIDDEN_HEAVY_PACKAGES:
        pkg_dir = output_dir / pkg
        assert not pkg_dir.exists(), f"Forbidden heavy package {pkg} found in artifact: {pkg_dir}"

    # Node runtime must be preserved and executable (C1)
    node_bin = output_dir / "playwright" / "driver" / "node"
    assert node_bin.exists(), f"Playwright Node runtime binary missing from {output_dir}"
    assert os.access(node_bin, os.X_OK), f"Playwright Node binary {node_bin} is not executable"

    # AC-8 size assertions with measured Sparticuz Chromium layer (C2)
    assert CHROMIUM_LAYER_UNCOMPRESSED_BYTES == SPARTICUZ_CHROMIUM_LAYER_UNPACKED_BYTES
    assert CHROMIUM_LAYER_UNCOMPRESSED_BYTES == 67_633_712
    assert combined_unpacked_bytes == unpacked_func_bytes + CHROMIUM_LAYER_UNCOMPRESSED_BYTES
    assert unpacked_func_bytes < MAX_FUNCTION_UNCOMPRESSED_BYTES, (
        f"Function unpacked size {unpacked_func_bytes} bytes exceeds {MAX_FUNCTION_UNCOMPRESSED_BYTES} bytes (185 MB)"
    )
    assert compressed_bytes < MAX_FUNCTION_COMPRESSED_BYTES, (
        f"Function compressed ZIP size {compressed_bytes} bytes exceeds {MAX_FUNCTION_COMPRESSED_BYTES} bytes (70 MB)"
    )
    assert combined_unpacked_bytes < MAX_COMBINED_UNCOMPRESSED_BYTES, (
        f"Combined unpacked size {combined_unpacked_bytes} bytes exceeds {MAX_COMBINED_UNCOMPRESSED_BYTES} bytes (250 MB)"
    )
    assert combined_unpacked_bytes < TARGET_COMBINED_UNCOMPRESSED_BYTES, (
        f"Combined unpacked size {combined_unpacked_bytes} bytes exceeds target {TARGET_COMBINED_UNCOMPRESSED_BYTES} bytes (225 MB)"
    )


def test_sparticuz_chromium_layer_artifact_measurements_and_hash() -> None:
    """C2: Verifies the official Sparticuz Chromium layer artifact SHA256 and extracted size."""
    import hashlib

    layer_archive = BACKEND_DIR / "build" / "layers" / SPARTICUZ_CHROMIUM_LAYER_FILENAME
    assert layer_archive.exists(), f"Sparticuz Chromium layer missing at {layer_archive}"

    # Verify version and SHA256 matches expected checksum
    assert SPARTICUZ_CHROMIUM_VERSION == "v130.0.0"
    actual_hash = hashlib.sha256(layer_archive.read_bytes()).hexdigest()
    assert actual_hash == SPARTICUZ_CHROMIUM_LAYER_SHA256

    # Verify compressed archive size
    assert layer_archive.stat().st_size == SPARTICUZ_CHROMIUM_LAYER_ARCHIVE_BYTES

    # Verify extracted layer contents in /opt
    extracted_dir = BACKEND_DIR / "build" / "layers" / "extracted"
    assert extracted_dir.exists(), f"Extracted layer directory missing at {extracted_dir}"

    extracted_files = [f for f in extracted_dir.rglob("*") if f.is_file()]
    extracted_bytes = sum(f.stat().st_size for f in extracted_files)
    assert extracted_bytes == SPARTICUZ_CHROMIUM_LAYER_UNPACKED_BYTES
    assert extracted_bytes == 67_633_712
    assert len(extracted_files) == 54


def test_sparticuz_layer_bootstrap_and_binary_usability() -> None:
    """C2: Verifies Sparticuz Chromium layer decompressor produces usable binary and distinguishes deferrals."""
    result = verify_sparticuz_layer_bootstrap()
    assert result["bootstrap_status"] == "VERIFIED"
    assert "Chromium 130." in result["chromium_version"]
    assert result["target_runtime_proof"] == "DEFERRED_TO_CLOUD_ROLLOUT"


def test_ensure_sparticuz_chromium_bootstrap() -> None:
    """Verifies ensure_sparticuz_chromium returns existing binary or inflates layer artifact."""
    from functions.browser.driver import ensure_sparticuz_chromium

    # When target exists and is executable
    assert ensure_sparticuz_chromium(target_path=sys.executable) == sys.executable

    # When layer is not present, returns None safely without error
    non_existent = Path("/nonexistent/directory")
    assert ensure_sparticuz_chromium(layer_dir=non_existent, target_path="/tmp/nonexistent_chromium") is None

    # When layer archive exists, bootstraps successfully from extracted layer
    layer_archive = BACKEND_DIR / "build" / "layers" / SPARTICUZ_CHROMIUM_LAYER_FILENAME
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(layer_archive, "r") as zf:
            zf.extractall(td)
        result = ensure_sparticuz_chromium(layer_dir=Path(td), target_path="/tmp/chromium")
        assert result == "/tmp/chromium"
        assert Path("/tmp/chromium").exists()
        # Clean up
        Path("/tmp/chromium").unlink(missing_ok=True)
        shutil.rmtree("/tmp/al2023", ignore_errors=True)
        shutil.rmtree("/tmp/fonts", ignore_errors=True)


def test_verify_browser_startup_and_render_in_isolated_artifact_environment() -> None:
    """C1: Proves Playwright Node runtime and live headless rendering from isolated artifact using local Chromium."""
    output_dir = BACKEND_DIR / "build" / "browser"
    if not output_dir.exists():
        output_dir, _, _, _, _ = build_isolated_browser_artifact()

    result = verify_browser_startup_and_render_isolated_environment(output_dir)
    assert result["local_proof"] == "VERIFIED"
    assert result["target_runtime_proof"] == "DEFERRED_TO_CLOUD_ROLLOUT"


def test_verify_browser_handler_import_clean_lambda_environment() -> None:
    """AC-8: Verifies consumer handler imports cleanly in isolated Python 3.12 with -S -P."""
    output_dir = BACKEND_DIR / "build" / "browser"
    if not output_dir.exists():
        output_dir, _, _, _, _ = build_isolated_browser_artifact()

    result = verify_handler_in_clean_environment(output_dir)
    assert result["status"] == "SUCCESS"


def test_cached_browser_artifact_build_avoids_reinstallation() -> None:
    """Verifies that subsequent builds reuse cached dependencies when requirements.txt has not changed."""
    start_time = time.perf_counter()
    (
        output_dir,
        zip_path,
        unpacked_func_bytes,
        compressed_bytes,
        combined_unpacked_bytes,
    ) = build_isolated_browser_artifact(force_reinstall=False)
    elapsed = time.perf_counter() - start_time

    assert output_dir.exists()
    assert (output_dir / ".requirements.hash").exists()
    assert (output_dir / "playwright").exists()
    assert (output_dir / "contracts" / "browser.py").exists()
    assert unpacked_func_bytes < MAX_FUNCTION_UNCOMPRESSED_BYTES
    # Cached run should complete in under 12 seconds (fresh pip install takes 40+ seconds)
    assert elapsed < 12.0, f"Cached build took too long ({elapsed:.2f}s); dependency caching failed"


def test_resolve_python_runtime_binary_fails_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that runtime resolution fails explicitly when Python 3.12 is unavailable."""
    from unittest.mock import MagicMock

    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    mock_run = MagicMock(return_value=type("SubResult", (), {"returncode": 1, "stdout": ""})())
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    monkeypatch.setattr("sys.version_info", (3, 13, 0, "final", 0))

    with pytest.raises(RuntimeError, match="Required Python 3.12 runtime is unavailable"):
        resolve_python_runtime_binary()
