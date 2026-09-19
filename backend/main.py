"""Convenience top-level entrypoint for running the SWENA FastAPI server.

You can run this file directly via:
    uv run python main.py
or:
    uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import sys
from pathlib import Path

# Ensure 'src' is on sys.path when executed directly from backend directory
src_dir = Path(__file__).resolve().parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

import uvicorn  # noqa: E402

from travel.runtime.fastapi.app import app  # noqa: E402

__all__ = ["app"]

if __name__ == "__main__":
    uvicorn.run(
        "travel.runtime.fastapi.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
