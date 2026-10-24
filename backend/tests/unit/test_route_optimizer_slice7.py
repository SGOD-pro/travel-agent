"""Unit test suite for route optimizer standalone packaging and Lambda limits (AC-7, AC-9).

Conforms strictly to specification 0005 Slice 7:
1. Validates SAM template structure, runtime configuration, and environment mappings.
2. Validates requirements.txt lean dependency boundaries (ortools present; playwright, fastapi, valhalla absent).
3. Verifies standalone ZIP and uncompressed artifact packaging within AWS Lambda size limits (<= 250 MB).
4. Verifies absence of bundled Valhalla binaries, graph tiles, and presentation frameworks.
5. Verifies isolated environment import execution of consumer handler.
6. Tests packaging CLI entrypoint and audit detection rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from functions.route_optimizer.packaging import (
    FORBIDDEN_APP_MODULES,
    MAX_COMPRESSED_BYTES,
    MAX_UNCOMPRESSED_BYTES,
    audit_package_contents,
    build_route_optimizer_artifact,
    verify_isolated_environment_imports,
)
from functions.route_optimizer.packaging import (
    main as packaging_main,
)

OPTIMIZER_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "functions" / "route_optimizer"
)
TEMPLATE_PATH = OPTIMIZER_DIR / "template.yaml"
REQUIREMENTS_PATH = OPTIMIZER_DIR / "requirements.txt"
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_cfn_template(path: Path) -> dict[str, Any]:
    """Loads CloudFormation SAM YAML with custom tag constructors."""

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


def test_sam_template_structure_and_settings_alignment() -> None:
    """Verifies that the SAM template exists and aligns runtime settings (AC-9)."""
    assert TEMPLATE_PATH.exists(), f"SAM template missing at {TEMPLATE_PATH}"

    template = _load_cfn_template(TEMPLATE_PATH)

    assert template.get("AWSTemplateFormatVersion") == "2010-09-09"
    assert template.get("Transform") == "AWS::Serverless-2016-10-31"

    # Verify Parameters
    params = template.get("Parameters", {})
    assert "Environment" in params
    assert params["Environment"]["AllowedValues"] == ["local", "staging", "production"]
    assert "DatabaseUrl" in params
    assert "OutboxQueueUrl" in params
    assert "ValhallaEndpointUrl" in params

    # Verify Globals Function configuration
    globals_sec = template.get("Globals", {})
    fn_globals = globals_sec.get("Function", {})
    assert fn_globals.get("Runtime") == "python3.12"
    assert fn_globals.get("Timeout") == 120
    assert fn_globals.get("MemorySize") == 1024

    env_vars = fn_globals.get("Environment", {}).get("Variables", {})
    assert "ENVIRONMENT" in env_vars
    assert "DATABASE_URL" in env_vars
    assert "OUTBOX_QUEUE_URL" in env_vars
    assert "SQS_QUEUE_URL" in env_vars
    assert "VALHALLA_ENDPOINT_URL" in env_vars
    assert "ORTOOLS_SOLVER_TIME_LIMIT_SECONDS" in env_vars
    assert "DEFAULT_PARKING_SEARCH_RADIUS_METERS" in env_vars

    # Verify Resources
    resources = template.get("Resources", {})
    assert "RouteOptimizerFunction" in resources
    assert "RouteOptimizerQueue" in resources
    assert "RouteOptimizerDLQ" in resources

    # Function configuration
    fn_props = resources["RouteOptimizerFunction"].get("Properties", {})
    assert fn_props.get("Handler") == "functions.route_optimizer.consumer.handler"
    assert fn_props.get("Runtime") == "python3.12"

    # Event configuration for SQS batch failure reporting
    events = fn_props.get("Events", {})
    assert "TaskDispatchBatch" in events
    batch_props = events["TaskDispatchBatch"].get("Properties", {})
    assert batch_props.get("BatchSize") == 5
    assert batch_props.get("FunctionResponseTypes") == ["ReportBatchItemFailures"]


def test_requirements_lean_boundaries_and_exclusions() -> None:
    """Verifies requirements.txt contains required solver dependencies and excludes heavy bloat."""
    assert REQUIREMENTS_PATH.exists(), f"requirements.txt missing at {REQUIREMENTS_PATH}"

    req_text = REQUIREMENTS_PATH.read_text(encoding="utf-8").lower()
    req_lines = [
        line.strip()
        for line in req_text.splitlines()
        if line.strip() and not line.startswith("#")
    ]

    # Required solver dependencies
    required_packages = ["ortools", "pydantic", "sqlalchemy", "shapely", "httpx", "asyncpg"]
    for pkg in required_packages:
        assert any(pkg in line for line in req_lines), f"Required package {pkg} missing from requirements.txt"

    # Forbidden heavy dependencies
    forbidden = ["playwright", "scrapling", "fastapi", "uvicorn", "reportlab", "valhalla", "langgraph"]
    for pkg in forbidden:
        assert not any(pkg in line for line in req_lines), f"Forbidden package {pkg} present in requirements.txt"


def test_standalone_packaging_size_and_compliance(tmp_path: Path) -> None:
    """Verifies that packaging produces an uncompressed artifact <= 250 MB and compressed <= 50 MB (AC-9)."""
    output_dir = tmp_path / "route_optimizer_artifact"

    metrics = build_route_optimizer_artifact(
        output_dir=output_dir,
        backend_root=BACKEND_DIR,
        install_dependencies=False,
    )

    # Size limits checks
    assert metrics["is_uncompressed_within_limit"] is True
    assert metrics["uncompressed_bytes"] <= MAX_UNCOMPRESSED_BYTES
    assert metrics["compressed_bytes"] <= MAX_COMPRESSED_BYTES
    assert metrics["uncompressed_mb"] < 250.0
    assert metrics["compressed_mb"] < 50.0

    # Compliance and auditing
    audit = metrics["audit"]
    assert audit["is_compliant"] is True
    assert audit["valhalla_detected"] is False
    assert len(audit["forbidden_detected"]) == 0
    assert metrics["is_compliant"] is True

    # Confirm key internal files are packaged
    assert (output_dir / "functions" / "route_optimizer" / "consumer.py").exists()
    assert (output_dir / "functions" / "route_optimizer" / "solver.py").exists()
    assert (output_dir / "functions" / "route_optimizer" / "valhalla.py").exists()
    assert (output_dir / "contracts" / "routing.py").exists()
    assert (output_dir / "config" / "config.py").exists()

    # Confirm forbidden app modules are NOT packaged
    for forbidden_mod in FORBIDDEN_APP_MODULES:
        forbidden_rel_path = forbidden_mod.replace(".", "/")
        assert not (output_dir / forbidden_rel_path).exists(), f"Forbidden module {forbidden_rel_path} was packaged"


def test_audit_package_contents_detects_forbidden_assets(tmp_path: Path) -> None:
    """Verifies audit_package_contents flags forbidden packages and unbundled Valhalla assets."""
    fake_artifact = tmp_path / "fake_pkg"
    fake_artifact.mkdir(parents=True)

    # Clean package initially
    (fake_artifact / "functions" / "route_optimizer").mkdir(parents=True)
    (fake_artifact / "functions" / "route_optimizer" / "valhalla.py").write_text("# client", encoding="utf-8")

    initial_audit = audit_package_contents(fake_artifact)
    assert initial_audit["is_compliant"] is True
    assert initial_audit["valhalla_detected"] is False

    # Inject forbidden presentation package
    fake_fastapi = fake_artifact / "fastapi"
    fake_fastapi.mkdir()
    (fake_fastapi / "__init__.py").write_text("# mock", encoding="utf-8")

    audit_with_fastapi = audit_package_contents(fake_artifact)
    assert audit_with_fastapi["is_compliant"] is False
    assert any("fastapi" in path for path in audit_with_fastapi["forbidden_detected"])

    # Clean up fastapi
    import shutil
    shutil.rmtree(fake_fastapi)

    # Inject unbundled Valhalla graph tiles or binary
    fake_tiles = fake_artifact / "valhalla_tiles"
    fake_tiles.mkdir()
    (fake_tiles / "tiles.tar").write_text("# graph data", encoding="utf-8")

    audit_with_tiles = audit_package_contents(fake_artifact)
    assert audit_with_tiles["is_compliant"] is False
    assert audit_with_tiles["valhalla_detected"] is True


def test_isolated_environment_import_verification(tmp_path: Path) -> None:
    """Verifies handler can be imported from an isolated artifact, and fails cleanly on empty dir."""
    output_dir = tmp_path / "valid_artifact"
    build_route_optimizer_artifact(output_dir, BACKEND_DIR, install_dependencies=False)

    # Valid artifact passes
    assert verify_isolated_environment_imports(output_dir) is True

    # Empty artifact fails import cleanly without uncaught exception
    empty_dir = tmp_path / "empty_artifact"
    empty_dir.mkdir()
    assert verify_isolated_environment_imports(empty_dir) is False


def test_packaging_cli_entrypoint(tmp_path: Path) -> None:
    """Verifies main entrypoint functions with CLI flags and returns status code 0."""
    target_dir = tmp_path / "cli_artifact"

    # Test main execution with explicit output-dir and backend-root
    exit_code = packaging_main(
        [
            "--output-dir",
            str(target_dir),
            "--backend-root",
            str(BACKEND_DIR),
        ]
    )
    assert exit_code == 0
    assert (target_dir / "functions" / "route_optimizer" / "consumer.py").exists()

    # Test verify-only mode
    verify_exit_code = packaging_main(
        [
            "--output-dir",
            str(target_dir),
            "--verify-only",
        ]
    )
    assert verify_exit_code == 0


def test_dependency_inclusive_python312_linux_artifact(tmp_path: Path) -> None:
    """Builds the dependency-inclusive Python 3.12 Linux artifact, measures it, and verifies imports + solve in clean runtime."""
    output_dir = tmp_path / "dep_inclusive_artifact"
    metrics = build_route_optimizer_artifact(
        output_dir=output_dir,
        backend_root=BACKEND_DIR,
        install_dependencies=True,
    )

    # 1. Size measurements: uncompressed must be <= 250 MB
    assert metrics["is_uncompressed_within_limit"] is True
    assert metrics["uncompressed_bytes"] <= MAX_UNCOMPRESSED_BYTES
    assert metrics["uncompressed_mb"] <= 250.0

    # 2. Package compliance and audit
    audit = metrics["audit"]
    assert audit["is_compliant"] is True
    assert audit["valhalla_detected"] is False
    assert len(audit["forbidden_detected"]) == 0

    # 3. Import verification and small solve in clean isolated runtime without repository files or dev site packages
    assert verify_isolated_environment_imports(output_dir, verify_solve=True) is True

