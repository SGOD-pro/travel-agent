#!/usr/bin/env bash
# ==============================================================================
# SWENA Sovereign Travel Intelligence Platform - Unified Startup Script
# ==============================================================================
# Usage:
#   ./start.sh           -> Starts both Backend (port 8000) and Frontend (port 3000)
#   ./start.sh all       -> Starts both Backend and Frontend concurrently
#   ./start.sh backend   -> Starts FastAPI backend using uv run uvicorn
#   ./start.sh frontend  -> Starts Next.js frontend using npm run dev
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MODE="${1:-all}"

# ANSI color codes for readable logging
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

run_backend() {
    echo -e "${CYAN}[SWENA Backend]${NC} Initializing FastAPI on http://0.0.0.0:8000 (uv + uvicorn)..."
    cd "$SCRIPT_DIR/backend"
    exec uv run uvicorn travel.runtime.fastapi.app:app --host 0.0.0.0 --port 8000 --reload
}

run_frontend() {
    echo -e "${GREEN}[SWENA Frontend]${NC} Initializing Next.js on http://localhost:3000 (npm run dev)..."
    cd "$SCRIPT_DIR/frontend"
    exec npm run dev
}

case "$MODE" in
    backend)
        run_backend
        ;;
    frontend)
        run_frontend
        ;;
    all|"")
        echo -e "${YELLOW}======================================================${NC}"
        echo -e "${GREEN}  SWENA Travel Intelligence Platform - Local Dev Stack${NC}"
        echo -e "${YELLOW}======================================================${NC}"
        echo -e "  - Backend:  ${CYAN}http://localhost:8000${NC} (FastAPI / Swagger: ${CYAN}http://localhost:8000/docs${NC})"
        echo -e "  - Frontend: ${GREEN}http://localhost:3000${NC} (Next.js 16 App Router + Tailwind v4)"
        echo -e "${YELLOW}======================================================${NC}"
        echo -e "Press Ctrl+C to shut down both servers.\n"

        # Trap cleanup on SIGINT and SIGTERM
        trap 'echo -e "\n${RED}Shutting down SWENA servers...${NC}"; kill 0; exit 0' SIGINT SIGTERM EXIT

        # Run backend in background
        (
            cd "$SCRIPT_DIR/backend"
            uv run uvicorn travel.runtime.fastapi.app:app --host 0.0.0.0 --port 8000 --reload
        ) &

        # Run frontend in background
        (
            cd "$SCRIPT_DIR/frontend"
            npm run dev
        ) &

        # Wait for all child processes
        wait
        ;;
    *)
        echo -e "${RED}Unknown mode: $MODE${NC}"
        echo "Usage: $0 [all|backend|frontend]"
        exit 1
        ;;
esac
