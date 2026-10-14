"""Evidence collector standalone packaging and artifact verification.

Builds an isolated deployment package containing:
1. Collector source code (functions/evidence_collector).
2. Permitted shared execution support (fencing, models, job_events, config, contracts).
3. Runtime dependencies installed from requirements.txt.
Excludes all forbidden modules (optimizer/OR-Tools, API/FastAPI, LangGraph, orchestrator policy/places).
Measures unpacked and compressed ZIP package sizes against AWS Lambda limits (250 MB unpacked, 50 MB compressed).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_COMPRESSED_BYTES: int = 50 * 1024 * 1024  # 50 MB
MAX_UNCOMPRESSED_BYTES: int = 250 * 1024 * 1024  # 250 MB

FORBIDDEN_HEAVY_PACKAGES: set[str] = {
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

FORBIDDEN_APP_MODULES: list[str] = [
    "functions.optimizer",
    "functions.api",
    "functions.orchestrator.evidence",
    "functions.orchestrator.places",
    "functions.orchestrator.graphs",
    "functions.orchestrator.checkpointer",
    "functions.browser",
]

PERMITTED_CONTRACT_FILES: tuple[str, ...] = (
    "evidence.py",
    "money.py",
    "trips.py",
)


def resolve_python_runtime_binary() -> str:
    """Finds a compatible Python runtime binary for building and verifying the artifact.

    Fails explicitly if Python 3.12 is not available.
    """
    # Check uv managed Python 3.12 installations
    candidates = [
        Path.home() / ".local" / "share" / "uv" / "python" / "cpython-3.12-linux-x86_64-gnu" / "bin" / "python3.12",
        Path.home() / ".local" / "share" / "uv" / "python" / "cpython-3.12.13-linux-x86_64-gnu" / "bin" / "python3.12",
    ]
    for cand in candidates:
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)

    # Try resolving via uv
    try:
        res = subprocess.run(
            ["uv", "python", "find", "3.12"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            found = res.stdout.strip()
            if os.path.exists(found):
                return found
    except Exception:
        pass

    # Check which python3.12 in PATH
    which_py = shutil.which("python3.12")
    if which_py and os.access(which_py, os.X_OK):
        return which_py

    # Check if active interpreter is Python 3.12
    if sys.version_info[:2] == (3, 12):
        return sys.executable

    raise RuntimeError(
        f"Required Python 3.12 runtime is unavailable. "
        f"Active interpreter is Python {sys.version_info[0]}.{sys.version_info[1]} ({sys.executable}). "
        f"Standalone collector artifact packaging and verification strictly requires Python 3.12."
    )


def build_isolated_collector_artifact(
    *,
    output_dir: Path | None = None,
    zip_path: Path | None = None,
    python_bin: str | Path | None = None,
    force_reinstall: bool = False,
) -> tuple[Path, Path, int, int]:
    """Builds an isolated artifact containing collector code, permitted shared support, and dependencies.

    Returns:
        tuple of (output_dir, zip_path, uncompressed_bytes, compressed_bytes)
    """
    base_file = Path(__file__).resolve()
    collector_dir = base_file.parent
    src_dir = collector_dir.parents[1]
    backend_dir = src_dir.parent
    requirements_path = collector_dir / "requirements.txt"

    if output_dir is None:
        output_dir = backend_dir / "build" / "evidence_collector"
    if zip_path is None:
        zip_path = backend_dir / "build" / "evidence_collector.zip"

    output_dir = output_dir.resolve()
    zip_path = zip_path.resolve()

    requirements_hash = hashlib.sha256(requirements_path.read_bytes()).hexdigest()
    hash_file = output_dir / ".requirements.hash"

    cached_dependencies_valid = (
        not force_reinstall
        and output_dir.exists()
        and (output_dir / "pydantic").exists()
        and hash_file.exists()
        and hash_file.read_text(encoding="utf-8").strip() == requirements_hash
    )

    runtime_python = str(python_bin) if python_bin else resolve_python_runtime_binary()

    if not cached_dependencies_valid:
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        zip_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. Install runtime dependencies via uv pip into target directory
        cmd = [
            "uv",
            "pip",
            "install",
            "--python",
            runtime_python,
            "--target",
            str(output_dir),
            "-r",
            str(requirements_path),
            "--link-mode=copy",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            # Fallback to standard pip if uv pip fails
            pip_cmd = [
                runtime_python,
                "-m",
                "pip",
                "install",
                "--target",
                str(output_dir),
                "-r",
                str(requirements_path),
            ]
            sub_res = subprocess.run(pip_cmd, capture_output=True, text=True, check=False)
            if sub_res.returncode != 0:
                raise RuntimeError(
                    f"Failed to install runtime dependencies into {output_dir}: {res.stderr}\n{sub_res.stderr}"
                )

        hash_file.write_text(requirements_hash, encoding="utf-8")
    else:
        # Clean out previously staged application source while preserving installed dependencies
        for staged in [output_dir / "functions", output_dir / "config", output_dir / "contracts"]:
            if staged.exists():
                shutil.rmtree(staged)
        zip_path.parent.mkdir(parents=True, exist_ok=True)

    # 2. Stage permitted application source files
    # Staging functions/evidence_collector
    dest_collector = output_dir / "functions" / "evidence_collector"
    dest_collector.mkdir(parents=True, exist_ok=True)
    (output_dir / "functions" / "__init__.py").write_text("", encoding="utf-8")
    for src_file in collector_dir.glob("*.py"):
        shutil.copy2(src_file, dest_collector / src_file.name)

    # Staging permitted 0001 execution support
    dest_exec = output_dir / "functions" / "orchestrator" / "execution"
    dest_exec.mkdir(parents=True, exist_ok=True)
    (output_dir / "functions" / "orchestrator" / "__init__.py").write_text("", encoding="utf-8")
    (dest_exec / "__init__.py").write_text("", encoding="utf-8")

    exec_src = src_dir / "functions" / "orchestrator" / "execution"
    for f_name in ["fencing.py", "models.py", "job_events.py"]:
        shutil.copy2(exec_src / f_name, dest_exec / f_name)

    # Staging config
    dest_config = output_dir / "config"
    dest_config.mkdir(parents=True, exist_ok=True)
    for f_name in ["__init__.py", "config.py", "db.py", "base_model.py"]:
        shutil.copy2(src_dir / "config" / f_name, dest_config / f_name)

    # Staging permitted contracts
    dest_contracts = output_dir / "contracts"
    dest_contracts.mkdir(parents=True, exist_ok=True)
    (dest_contracts / "__init__.py").write_text("", encoding="utf-8")
    for c_name in PERMITTED_CONTRACT_FILES:
        c_path = src_dir / "contracts" / c_name
        if c_path.exists():
            shutil.copy2(c_path, dest_contracts / c_name)

    # 3. Clean up stray __pycache__ and test files
    for pycache in output_dir.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)
    for pyc in output_dir.rglob("*.pyc"):
        try:
            pyc.unlink()
        except OSError:
            pass

    # 4. Strict isolation verification
    for forbidden in FORBIDDEN_HEAVY_PACKAGES:
        direct_pkg = output_dir / forbidden
        assert not direct_pkg.exists(), f"Forbidden package {forbidden} found in artifact: {direct_pkg}"

    assert not (output_dir / "functions" / "optimizer").exists(), "optimizer must not exist in collector artifact"
    assert not (output_dir / "functions" / "api").exists(), "api must not exist in collector artifact"
    assert not (output_dir / "functions" / "orchestrator" / "evidence").exists(), (
        "orchestrator evidence policy must not exist in collector artifact"
    )
    assert not (output_dir / "functions" / "orchestrator" / "places").exists(), (
        "orchestrator places must not exist in collector artifact"
    )
    assert not (output_dir / "functions" / "orchestrator" / "graphs").exists(), (
        "orchestrator graphs must not exist in collector artifact"
    )

    # 5. Measure unpacked size
    unpacked_bytes = sum(f.stat().st_size for f in output_dir.rglob("*") if f.is_file())
    if unpacked_bytes > MAX_UNCOMPRESSED_BYTES:
        raise ValueError(
            f"Unpacked artifact size ({unpacked_bytes} bytes) exceeds limit of {MAX_UNCOMPRESSED_BYTES} bytes"
        )

    # 6. Build compressed ZIP archive
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(output_dir):
            for f in files:
                if f.startswith(".requirements"):
                    continue
                full_path = Path(root) / f
                rel_path = full_path.relative_to(output_dir)
                zf.write(full_path, str(rel_path))

    compressed_bytes = zip_path.stat().st_size
    if compressed_bytes > MAX_COMPRESSED_BYTES:
        raise ValueError(
            f"Compressed ZIP size ({compressed_bytes} bytes) exceeds limit of {MAX_COMPRESSED_BYTES} bytes"
        )

    return output_dir, zip_path, unpacked_bytes, compressed_bytes


def verify_handler_in_clean_environment(
    artifact_dir: Path,
    python_bin: str | Path | None = None,
) -> dict[str, Any]:
    """Spawns an isolated Python process with ONLY artifact_dir on PYTHONPATH to verify handler imports."""
    runtime_python = str(python_bin) if python_bin else resolve_python_runtime_binary()

    verify_script = """
import os, sys, platform

# 1. Confirm runtime and architecture compatibility
assert sys.version_info[:2] == (3, 12), f"Expected Python 3.12 runtime, got {sys.version}"
assert platform.machine().lower() in ("x86_64", "amd64"), f"Expected x86_64 architecture, got {platform.machine()}"

# 2. Confirm dependencies come only from artifact and site-packages is completely excluded
for path_entry in sys.path:
    assert ".venv" not in path_entry, f"Host virtualenv path found in sys.path: {path_entry}"
    assert "site-packages" not in path_entry, f"Forbidden site-packages found in sys.path: {path_entry}"

# Verify standard library paths are retained and artifact directory is present
assert any("lib/python3.12" in p or "lib-dynload" in p for p in sys.path), "Standard library missing from sys.path"
assert any(os.path.realpath(p) == os.path.realpath(os.environ["PYTHONPATH"]) for p in sys.path), "Artifact dir missing from sys.path"

# 3. Verify handler import from isolated artifact
from functions.evidence_collector.consumer import lambda_handler, process_batch, process_record
assert callable(lambda_handler), "lambda_handler must be callable"
assert callable(process_batch), "process_batch must be callable"
assert callable(process_record), "process_record must be callable"

# 4. Verify all forbidden heavy and orchestrator modules fail to import
forbidden_modules = [
    "functions.optimizer",
    "functions.api",
    "functions.orchestrator.evidence",
    "functions.orchestrator.places",
    "functions.orchestrator.graphs",
    "functions.browser",
    "ortools",
    "langgraph",
    "playwright",
]

for mod in forbidden_modules:
    try:
        __import__(mod)
        raise AssertionError(f"Forbidden module {mod} was imported unexpectedly!")
    except ModuleNotFoundError:
        pass

print("OK")
"""

    clean_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(artifact_dir.resolve()),
        "PYTHONNOUSERSITE": "1",
        "ENVIRONMENT": "staging",
        "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost:5432/travel_planning",
        "SQS_QUEUE_URL": "https://sqs.ap-south-1.amazonaws.com/123/evidence-queue",
        "SERPAPI_API_KEY": "test_api_key",
        "AWS_REGION": "ap-south-1",
        "SERPAPI_BASE_URL": "https://serpapi.com",
        "HTTP_REQUEST_TIMEOUT_SECONDS": "20",
        "MAX_SOURCE_PAYLOAD_BYTES": "5242880",
    }

    res = subprocess.run(
        [runtime_python, "-S", "-P", "-c", verify_script],
        cwd=str(artifact_dir.resolve()),
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"Handler verification failed in clean environment:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
        )

    return {"status": "SUCCESS", "stdout": res.stdout.strip()}


if __name__ == "__main__":
    out_dir, z_path, uncompressed, compressed = build_isolated_collector_artifact()
    print(f"Artifact built at: {out_dir}")
    print(f"ZIP package at:    {z_path}")
    print(f"Unpacked size:     {uncompressed / (1024 * 1024):.2f} MB")
    print(f"Compressed size:   {compressed / (1024 * 1024):.2f} MB")
    verify_res = verify_handler_in_clean_environment(out_dir)
    print(f"Verification:      {verify_res['status']}")
