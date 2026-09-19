"""Package-level entrypoint for the SWENA FastAPI server.

Can be run via:
    uv run python -m travel.main
or:
    uv run uvicorn travel.main:app --reload
"""

import uvicorn

from travel.runtime.fastapi.app import app

__all__ = ["app"]

if __name__ == "__main__":
    uvicorn.run(
        "travel.runtime.fastapi.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
