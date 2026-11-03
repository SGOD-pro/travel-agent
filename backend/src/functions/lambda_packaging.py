"""Helpers for building and checking Linux Lambda artifacts from Windows."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

LAMBDA_BUILD_IMAGE = "python:3.12-slim"
LAMBDA_TARGET_FINGERPRINT = b"linux/amd64-python3.12-v2"


def install_lambda_requirements(
    output_dir: Path,
    requirements_path: Path,
    runtime_python: str,
) -> None:
    """Install dependencies for the Lambda Linux runtime into an artifact directory."""
    if os.name != "nt":
        result = subprocess.run(
            [
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
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return

        fallback = subprocess.run(
            [
                runtime_python,
                "-m",
                "pip",
                "install",
                "--target",
                str(output_dir),
                "-r",
                str(requirements_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if fallback.returncode == 0:
            return
        raise RuntimeError(
            f"Failed to install runtime dependencies into {output_dir}: "
            f"{result.stderr}\n{fallback.stderr}"
        )

    if shutil.which("docker") is None:
        raise RuntimeError(
            "Docker is required to build Linux Lambda artifacts from Windows. "
            "Start Docker Desktop with Linux containers enabled and retry."
        )

    has_playwright = any(
        line.strip().lower().startswith(b"playwright")
        for line in requirements_path.read_bytes().splitlines()
        if line.strip() and not line.lstrip().startswith(b"#")
    )
    install_command = [
        "python",
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--target",
        "/var/task",
        "-r",
        "/tmp/requirements.txt",
    ]
    if has_playwright:
        install_command = [
            "sh",
            "-c",
            "apt-get update && apt-get install -y --no-install-recommends binutils "
            "&& rm -rf /var/lib/apt/lists/* && python -m pip install --no-cache-dir "
            "--target /var/task -r /tmp/requirements.txt "
            "&& strip /var/task/playwright/driver/node "
            "&& chmod 755 /var/task/playwright/driver/node "
            "&& find /var/task -type f -name '*.so' -exec strip --strip-unneeded {} +",
        ]
    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--mount",
        f"type=bind,source={requirements_path.resolve().as_posix()},target=/tmp/requirements.txt,readonly",
        "--mount",
        f"type=bind,source={output_dir.resolve().as_posix()},target=/var/task",
        LAMBDA_BUILD_IMAGE,
        *install_command,
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to install Linux Lambda dependencies into {output_dir}: "
            f"{result.stdout}\n{result.stderr}"
        )
def run_lambda_python(
    script: str,
    artifact_dir: Path,
    env: dict[str, str],
    runtime_python: str,
) -> subprocess.CompletedProcess[str]:
    """Run an isolated Python verification using the Lambda Linux runtime on Windows."""
    if os.name != "nt":
        return subprocess.run(
            [runtime_python, "-S", "-P", "-c", script],
            cwd=str(artifact_dir.resolve()),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--mount",
        f"type=bind,source={artifact_dir.resolve().as_posix()},target=/var/task",
        "--workdir",
        "/var/task",
    ]
    for key, value in env.items():
        if key == "PATH":
            continue
        if key == "PYTHONPATH":
            value = "/var/task"
        command.extend(["--env", f"{key}={value}"])
    command.extend(
        [
            LAMBDA_BUILD_IMAGE,
            "python",
            "-S",
            "-P",
            "-c",
            script,
        ]
    )
    return subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
