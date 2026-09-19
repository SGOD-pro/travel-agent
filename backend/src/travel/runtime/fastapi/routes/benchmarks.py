"""API routes for automated benchmark suites and system evaluations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from travel.benchmarks.runner import BenchmarkRunner

router = APIRouter(prefix="/api/v1/benchmarks", tags=["benchmarks"])


@router.get("")
def run_benchmarks() -> dict[str, Any]:
    """Execute the 50-scenario benchmark suite against invariant rules and return verification results."""
    runner = BenchmarkRunner()
    return runner.run_all()
