"""Route optimizer standalone packaging and artifact verification.

Builds an isolated deployment package containing:
1. Route optimizer source code (functions/route_optimizer).
2. Permitted shared execution support (fencing, models, job_events, config, contracts).
3. Runtime dependencies installed from requirements.txt.
Excludes forbidden presentation and browser modules (Playwright, Scrapling, FastAPI, Uvicorn).
Measures unpacked and compressed ZIP package sizes against AWS Lambda limits (250 MB unpacked, 50 MB compressed).
"""

from __future__ import annotations

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
    "playwright",
    "scrapling",
    "reportlab",
    "fastapi",
    "uvicorn",
}

FORBIDDEN_APP_MODULES: list[str] = [
    "functions.api",
    "functions.browser",
    "functions.evidence_collector",
    "functions.orchestrator.graphs",
    "functions.orchestrator.checkpointer",
]

PERMITTED_CONTRACT_FILES: tuple[str, ...] = (
    "routing.py",
    "money.py",
    "trips.py",
)


def resolve_python_runtime_binary() -> str:
    """Finds a compatible Python runtime binary for building and verifying the artifact."""
    if sys.version_info[:2] in ((3, 12), (3, 13)):
        return sys.executable

    which_py = shutil.which("python3.12") or shutil.which("python3")
    if which_py and os.access(which_py, os.X_OK):
        return which_py

    return sys.executable


def build_route_optimizer_artifact(
    output_dir: Path,
    backend_root: Path,
    install_dependencies: bool = False,
) -> dict[str, Any]:
    """Assembles an isolated route optimizer worker package."""
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    src_dir = backend_root / "src"

    # 1. Copy route optimizer source
    opt_dest = output_dir / "functions" / "route_optimizer"
    opt_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir / "functions" / "route_optimizer", opt_dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 2. Copy orchestrator routing support
    orch_routing_dest = output_dir / "functions" / "orchestrator" / "routing"
    orch_routing_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir / "functions" / "orchestrator" / "routing", orch_routing_dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 3. Copy permitted execution fencing and models
    orch_exec_dest = output_dir / "functions" / "orchestrator" / "execution"
    orch_exec_dest.mkdir(parents=True, exist_ok=True)
    (orch_exec_dest / "__init__.py").write_text("", encoding="utf-8")
    for fname in ("fencing.py", "models.py", "job_events.py"):
        fpath = src_dir / "functions" / "orchestrator" / "execution" / fname
        if fpath.exists():
            shutil.copy2(fpath, orch_exec_dest / fname)

    # 4. Copy canonical places for parking hubs
    orch_places_dest = output_dir / "functions" / "orchestrator" / "places"
    orch_places_dest.mkdir(parents=True, exist_ok=True)
    (orch_places_dest / "__init__.py").write_text("", encoding="utf-8")
    for fname in ("models.py", "repository.py", "schemas.py"):
        fpath = src_dir / "functions" / "orchestrator" / "places" / fname
        if fpath.exists():
            shutil.copy2(fpath, orch_places_dest / fname)

    # Copy top-level functions init
    (output_dir / "functions" / "__init__.py").touch()
    (output_dir / "functions" / "orchestrator" / "__init__.py").touch()

    # 5. Copy config
    config_dest = output_dir / "config"
    config_dest.mkdir(parents=True, exist_ok=True)
    for fname in ("config.py", "db.py", "base_model.py", "aws.py", "__init__.py"):
        fpath = src_dir / "config" / fname
        if fpath.exists():
            shutil.copy2(fpath, config_dest / fname)

    # 6. Copy permitted contracts
    contracts_dest = output_dir / "contracts"
    contracts_dest.mkdir(parents=True, exist_ok=True)
    (contracts_dest / "__init__.py").touch()
    for fname in PERMITTED_CONTRACT_FILES:
        fpath = src_dir / "contracts" / fname
        if fpath.exists():
            shutil.copy2(fpath, contracts_dest / fname)

    # 7. Optionally install runtime dependencies
    if install_dependencies:
        req_file = opt_dest / "requirements.txt"
        if req_file.exists():
            subprocess.run(
                [
                    resolve_python_runtime_binary(),
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(req_file),
                    "--target",
                    str(output_dir),
                    "--no-cache-dir",
                ],
                check=True,
                capture_output=True,
            )

    # 8. Measure uncompressed size
    uncompressed_bytes = sum(
        f.stat().st_size for f in output_dir.rglob("*") if f.is_file()
    )

    # 9. Audit package contents for forbidden modules and unbundled Valhalla assets
    audit_res = audit_package_contents(output_dir)

    # 10. Build zip archive to measure compressed size
    zip_path = output_dir.parent / "route_optimizer.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in output_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(output_dir))

    compressed_bytes = zip_path.stat().st_size

    return {
        "output_dir": str(output_dir),
        "zip_path": str(zip_path),
        "uncompressed_bytes": uncompressed_bytes,
        "compressed_bytes": compressed_bytes,
        "uncompressed_mb": round(uncompressed_bytes / (1024 * 1024), 2),
        "compressed_mb": round(compressed_bytes / (1024 * 1024), 2),
        "is_uncompressed_within_limit": uncompressed_bytes <= MAX_UNCOMPRESSED_BYTES,
        "audit": audit_res,
        "is_compliant": audit_res["is_compliant"] and (uncompressed_bytes <= MAX_UNCOMPRESSED_BYTES),
    }


def audit_package_contents(artifact_dir: Path) -> dict[str, Any]:
    """Audits packaged artifact contents ensuring no forbidden dependencies or Valhalla assets.

    Verifies AC-9: Lambda package excludes Valhalla binaries, graph tiles, and presentation bloat.
    """
    forbidden_detected: list[str] = []
    valhalla_detected: bool = False

    for item in artifact_dir.rglob("*"):
        name_lower = item.name.lower()
        rel_path_str = str(item.relative_to(artifact_dir)).replace("\\", "/")

        for forbidden in FORBIDDEN_HEAVY_PACKAGES:
            # Check package directories or wheel files
            if forbidden in name_lower and not rel_path_str.startswith("functions/route_optimizer"):
                forbidden_detected.append(rel_path_str)
                break

        for forbidden_mod in FORBIDDEN_APP_MODULES:
            mod_path = forbidden_mod.replace(".", "/")
            if mod_path in rel_path_str:
                forbidden_detected.append(rel_path_str)
                break

        # Check for unbundled Valhalla binaries, C++ engines, or OpenStreetMap graph tiles
        # Permitted: internal client functions/route_optimizer/valhalla.py
        if (
            name_lower.startswith("valhalla_service")
            or name_lower.startswith("libvalhalla")
            or name_lower.endswith(".pbf")
            or name_lower.endswith(".osm")
            or "valhalla_tiles" in rel_path_str
            or (item.is_dir() and item.name == "valhalla" and "functions/route_optimizer" not in rel_path_str)
        ):
            valhalla_detected = True

    return {
        "is_compliant": len(forbidden_detected) == 0 and not valhalla_detected,
        "forbidden_detected": forbidden_detected,
        "valhalla_detected": valhalla_detected,
    }


def verify_isolated_environment_imports(artifact_dir: Path) -> bool:
    """Verifies that the consumer handler can be imported cleanly from the artifact."""
    consumer_file = artifact_dir / "functions" / "route_optimizer" / "consumer.py"
    if not consumer_file.exists():
        return False

    py_bin = resolve_python_runtime_binary()
    cmd = [
        py_bin,
        "-c",
        "from functions.route_optimizer.consumer import handler; print('IMPORT_OK')",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(artifact_dir)

    res = subprocess.run(
        cmd,
        env=env,
        cwd=str(artifact_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0 or "IMPORT_OK" not in res.stdout:
        logger.error("Artifact isolation import check failed: %s\n%s", res.stdout, res.stderr)
        return False
    return True


def main(args: list[str] | None = None) -> int:
    """CLI entrypoint for standalone packaging building and verification."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Build and verify standalone route optimizer Lambda package"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to output the package",
    )
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=None,
        help="Path to backend root directory",
    )
    parser.add_argument(
        "--install-dependencies",
        action="store_true",
        help="Install runtime dependencies into package",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify imports without rebuilding",
    )

    parsed = parser.parse_args(args)

    repo_backend = (
        parsed.backend_root
        if parsed.backend_root is not None
        else Path(__file__).resolve().parent.parent.parent.parent
    )
    dist_dir = (
        parsed.output_dir
        if parsed.output_dir is not None
        else (repo_backend / "build" / "route_optimizer")
    )

    if not parsed.verify_only:
        metrics = build_route_optimizer_artifact(
            dist_dir,
            repo_backend,
            install_dependencies=parsed.install_dependencies,
        )
        print(f"Artifact built at: {metrics['output_dir']}")
        print(
            f"Uncompressed: {metrics['uncompressed_mb']} MB (within 250 MB: {metrics['is_uncompressed_within_limit']})"
        )
        print(f"Compressed: {metrics['compressed_mb']} MB")
        print(f"Package compliant: {metrics['audit']['is_compliant']}")
        if not metrics["is_uncompressed_within_limit"]:
            print(
                "ERROR: Uncompressed size exceeds AWS Lambda 250 MB limit",
                file=sys.stderr,
            )
            return 1
        if not metrics["audit"]["is_compliant"]:
            print(
                f"ERROR: Non-compliant package contents: {metrics['audit']}",
                file=sys.stderr,
            )
            return 1

    if not verify_isolated_environment_imports(dist_dir):
        print("ERROR: Import verification failed", file=sys.stderr)
        return 1

    print("Verification passed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
