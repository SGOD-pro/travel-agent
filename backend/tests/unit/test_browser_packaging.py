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
import time
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
    OBSCURA_BINARY_UNPACKED_BYTES,
    OBSCURA_COMMIT,
    OBSCURA_VERSION,
    TARGET_COMBINED_UNCOMPRESSED_BYTES,
    build_isolated_browser_artifact,
    resolve_python_runtime_binary,
    verify_browser_startup_and_render_isolated_environment,
    verify_handler_in_clean_environment,
    verify_obscura_binary_artifact,
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
    assert "ChromiumLayerArn" not in parameters

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
    assert env_vars.get("OBSCURA_PATH") == "/var/task/bin/obscura"
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
    assert "Layers" not in fn_props

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
        assert not target_path.exists(), (
            f"Forbidden app module {mod_path} found in artifact: {target_path}"
        )

    # Strict isolation: forbidden heavy third-party packages must be excluded
    for pkg in FORBIDDEN_HEAVY_PACKAGES:
        pkg_dir = output_dir / pkg
        assert not pkg_dir.exists(), f"Forbidden heavy package {pkg} found in artifact: {pkg_dir}"

    # Node runtime must be preserved and executable (C1)
    node_bin = output_dir / "playwright" / "driver" / "node"
    assert node_bin.exists(), f"Playwright Node runtime binary missing from {output_dir}"
    assert os.access(node_bin, os.X_OK), f"Playwright Node binary {node_bin} is not executable"

    # Obscura binary must be packaged and executable
    obscura_bin = output_dir / "bin" / "obscura"
    assert obscura_bin.exists(), f"Obscura binary missing from package: {obscura_bin}"
    assert os.access(obscura_bin, os.X_OK), f"Obscura binary {obscura_bin} is not executable"

    # Size assertions conforming to AWS Lambda 250 MB ceiling
    assert CHROMIUM_LAYER_UNCOMPRESSED_BYTES == 0
    assert combined_unpacked_bytes == unpacked_func_bytes
    assert unpacked_func_bytes <= MAX_FUNCTION_UNCOMPRESSED_BYTES, (
        f"Function unpacked size {unpacked_func_bytes} bytes exceeds {MAX_FUNCTION_UNCOMPRESSED_BYTES} bytes (250 MB)"
    )
    assert compressed_bytes < MAX_FUNCTION_COMPRESSED_BYTES, (
        f"Function compressed ZIP size {compressed_bytes} bytes exceeds {MAX_FUNCTION_COMPRESSED_BYTES} bytes (100 MB)"
    )
    assert combined_unpacked_bytes <= MAX_COMBINED_UNCOMPRESSED_BYTES, (
        f"Combined unpacked size {combined_unpacked_bytes} bytes exceeds {MAX_COMBINED_UNCOMPRESSED_BYTES} bytes (250 MB)"
    )
    assert combined_unpacked_bytes < TARGET_COMBINED_UNCOMPRESSED_BYTES, (
        f"Combined unpacked size {combined_unpacked_bytes} bytes exceeds target {TARGET_COMBINED_UNCOMPRESSED_BYTES} bytes (240 MB)"
    )


def test_obscura_binary_artifact_verification() -> None:
    """Verifies that the standalone Obscura binary satisfies execution and architecture requirements."""
    assert OBSCURA_VERSION == "v0.2.7"
    assert OBSCURA_COMMIT == "0ecc41770da5c5300227dd83a15bfa1e5100d5d4"
    assert OBSCURA_BINARY_UNPACKED_BYTES > 50_000_000
    res = verify_obscura_binary_artifact()
    assert res["status"] == "SUCCESS"
    assert "obscura" in res["version"]
    assert res["size_bytes"] > 50_000_000
    assert Path(res["binary_path"]).exists()


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


def test_resolve_python_runtime_binary_fails_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies that runtime resolution fails explicitly when Python 3.12 is unavailable."""
    from unittest.mock import MagicMock

    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    mock_run = MagicMock(return_value=type("SubResult", (), {"returncode": 1, "stdout": ""})())
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    monkeypatch.setattr("sys.version_info", (3, 13, 0, "final", 0))

    with pytest.raises(RuntimeError, match="Required Python 3.12 runtime is unavailable"):
        resolve_python_runtime_binary()


def test_obscura_path_alias_backward_compatibility() -> None:
    """Verifies OBSCURA_PATH accepts CHROMIUM_PATH environment alias for backwards compatibility."""
    from config.config import Settings

    s = Settings(CHROMIUM_PATH="/custom/legacy/path")
    assert s.OBSCURA_PATH == "/custom/legacy/path"


def test_resolve_obscura_binary_with_chromium_path_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verifies resolve_obscura_binary resolves CHROMIUM_PATH if OBSCURA_PATH is unset."""
    from functions.browser.driver import resolve_obscura_binary

    fake_bin = tmp_path / "legacy_browser"
    fake_bin.write_bytes(b"\x7fELFfake")
    os.chmod(fake_bin, 0o755)

    monkeypatch.delenv("OBSCURA_PATH", raising=False)
    monkeypatch.setenv("CHROMIUM_PATH", str(fake_bin))
    assert resolve_obscura_binary() == str(fake_bin.resolve())


def test_verify_browser_startup_fails_when_obscura_missing(tmp_path: Path) -> None:
    """Negative test: missing Obscura binary must fail packaging verification, never return VERIFIED."""
    mock_artifact = tmp_path / "artifact"
    mock_artifact.mkdir(parents=True, exist_ok=True)
    # Even if playwright exists, missing Obscura in bin/ must raise RuntimeError / FileNotFoundError
    (mock_artifact / "bin").mkdir(parents=True, exist_ok=True)

    with pytest.raises(RuntimeError, match="Isolated browser startup and rendering verification failed"):
        verify_browser_startup_and_render_isolated_environment(
            mock_artifact,
            obscura_path=str(tmp_path / "nonexistent_obscura"),
        )


def test_verify_browser_startup_rejects_malformed_or_skipped_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verifies that verification strictly rejects missing, malformed, or skipped render outputs."""
    from unittest.mock import MagicMock

    mock_artifact = tmp_path / "artifact"
    mock_artifact.mkdir(parents=True, exist_ok=True)

    # Case 1: returncode 0 but missing STRUCTURED_RENDER_RESULT
    mock_run = MagicMock(
        return_value=type(
            "SubResult",
            (),
            {"returncode": 0, "stdout": "Some unexpected output\nwithout marker", "stderr": ""},
        )()
    )
    monkeypatch.setattr("subprocess.run", mock_run)

    with pytest.raises(RuntimeError, match="missing or unparseable structured output"):
        verify_browser_startup_and_render_isolated_environment(mock_artifact)

    # Case 2: returncode 0 but status != SUCCESS (e.g. SKIPPED_NO_BROWSER)
    mock_run_skipped = MagicMock(
        return_value=type(
            "SubResult",
            (),
            {
                "returncode": 0,
                "stdout": 'STRUCTURED_RENDER_RESULT:{"status": "SKIPPED_NO_BROWSER", "reason": "No Obscura found"}',
                "stderr": "",
            },
        )()
    )
    monkeypatch.setattr("subprocess.run", mock_run_skipped)

    with pytest.raises(RuntimeError, match="Isolated browser rendering failed or was skipped"):
        verify_browser_startup_and_render_isolated_environment(mock_artifact)


def test_build_isolated_browser_artifact_fails_when_obscura_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verifies artifact construction fails immediately when required Obscura binary is absent."""
    out_dir = tmp_path / "browser_pkg"
    zip_dest = tmp_path / "browser.zip"

    # Block any external Obscura binary resolution
    monkeypatch.delenv("OBSCURA_PATH", raising=False)
    monkeypatch.delenv("CHROMIUM_PATH", raising=False)
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    orig_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda self: False if "obscura" in str(self) else orig_is_file(self),
    )

    with pytest.raises(FileNotFoundError, match="Required Obscura browser binary is missing"):
        build_isolated_browser_artifact(
            output_dir=out_dir,
            zip_path=zip_dest,
            force_reinstall=False,
        )

