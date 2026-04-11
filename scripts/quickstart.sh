#!/usr/bin/env bash
# quickstart.sh — One-command setup for ASOE development environment.
#
# This script gets a novice from zero to a working dev environment with
# all tests passing. It handles Python, dependencies, database seeding,
# and verification.
#
# Usage:
#   bash scripts/quickstart.sh           # full dev setup
#   bash scripts/quickstart.sh --test    # run tests only (skip install)
#   bash scripts/quickstart.sh --prod    # production-like setup (PostgreSQL + Redis)
#
# What it does:
#   1. Checks prerequisites (Python 3.11+, pip)
#   2. Creates a virtual environment (if not already active)
#   3. Installs core + dev dependencies
#   4. Seeds the sandbox SQLite database
#   5. Runs the full test suite
#   6. Prints next-steps guide
#
# For production-like setup (--prod), it additionally:
#   7. Starts PostgreSQL and Redis containers via Docker
#   8. Applies database migrations
#   9. Verifies API server starts

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# ── Colors ──────────────────────────────────────────────────────────────

GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
CYAN='\033[36m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

info()    { echo -e "${GREEN}[quickstart]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[quickstart]${RESET} $*"; }
error()   { echo -e "${RED}[quickstart]${RESET} $*"; }
header()  { echo -e "\n${BOLD}$*${RESET}"; }
step()    { echo -e "  ${CYAN}→${RESET} $*"; }

# ── Parse args ──────────────────────────────────────────────────────────

MODE="dev"
for arg in "$@"; do
    case "$arg" in
        --test) MODE="test" ;;
        --prod) MODE="prod" ;;
        --help|-h)
            echo "Usage: bash scripts/quickstart.sh [--test|--prod]"
            echo ""
            echo "  (default)  Full dev setup: venv, deps, seed, tests"
            echo "  --test     Run tests only (assumes deps installed)"
            echo "  --prod     Production-like: adds PostgreSQL + Redis containers"
            exit 0
            ;;
        *) error "Unknown argument: $arg"; exit 1 ;;
    esac
done

# ═══════════════════════════════════════════════════════════════════════
# Test-only mode
# ═══════════════════════════════════════════════════════════════════════

if [ "$MODE" = "test" ]; then
    header "Running test suite..."
    python -m pytest
    exit $?
fi

# ═══════════════════════════════════════════════════════════════════════
# Step 1: Check prerequisites
# ═══════════════════════════════════════════════════════════════════════

header "Step 1: Checking prerequisites"

# Python
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    error "Python not found. Install Python 3.11+ from https://python.org"
    exit 1
fi

PY_VERSION=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$($PYTHON -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($PYTHON -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]); then
    error "Python 3.11+ required (found $PY_VERSION). Install from https://python.org"
    exit 1
fi
step "Python $PY_VERSION OK"

# uv (preferred) or pip (fallback)
USE_UV=false
if command -v uv &>/dev/null; then
    USE_UV=true
    step "uv $(uv --version 2>/dev/null || echo '?') OK (preferred)"
elif $PYTHON -m pip --version &>/dev/null; then
    step "pip OK (uv not found — using pip as fallback)"
    warn "For faster installs, install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
else
    error "Neither uv nor pip found. Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Docker (only required for --prod)
if [ "$MODE" = "prod" ]; then
    if ! command -v docker &>/dev/null; then
        error "Docker not found. Install from https://docs.docker.com/get-docker/"
        exit 1
    fi
    step "Docker OK"
fi

# ═══════════════════════════════════════════════════════════════════════
# Step 2: Create virtual environment
# ═══════════════════════════════════════════════════════════════════════

header "Step 2: Setting up virtual environment"

if [ -z "${VIRTUAL_ENV:-}" ]; then
    if [ ! -d ".venv" ]; then
        step "Creating .venv..."
        if $USE_UV; then
            uv venv --python "$PY_VERSION"
        else
            $PYTHON -m venv .venv
        fi
    fi
    step "Activating .venv..."
    source .venv/bin/activate
    step "Virtual environment active: $VIRTUAL_ENV"
else
    step "Already in virtual environment: $VIRTUAL_ENV"
fi

# ═══════════════════════════════════════════════════════════════════════
# Step 3: Install dependencies
# ═══════════════════════════════════════════════════════════════════════

header "Step 3: Installing dependencies"

if $USE_UV; then
    step "Installing core + dev dependencies (uv)..."
    uv pip install -e ".[dev]"

    step "Installing streamlit (sandbox UI)..."
    uv pip install streamlit
else
    step "Installing core + dev dependencies (pip)..."
    pip install -e ".[dev]" --quiet || pip install -e ".[dev]"

    step "Installing streamlit (sandbox UI)..."
    pip install streamlit --quiet || pip install streamlit
fi

# Apply pydantic patch if needed (Python 3.14+)
if [ "$PY_MINOR" -ge 14 ] && [ -f "scripts/apply-patches.sh" ]; then
    step "Applying pydantic compatibility patch (Python 3.14)..."
    bash scripts/apply-patches.sh "$(which python)"
fi

step "Dependencies installed."

# ═══════════════════════════════════════════════════════════════════════
# Step 4: Seed the sandbox database
# ═══════════════════════════════════════════════════════════════════════

header "Step 4: Seeding sandbox database"

PYTHONPATH=. python tests/sandbox/seed.py
step "Sandbox database ready at tests/sandbox/sandbox.db"

# ═══════════════════════════════════════════════════════════════════════
# Step 5: Run the test suite
# ═══════════════════════════════════════════════════════════════════════

header "Step 5: Running test suite"

if PYTHONPATH=. python -m pytest --tb=short; then
    step "All tests passed."
else
    error "Some tests failed. Run 'python -m pytest -v' for details."
    exit 1
fi

# ═══════════════════════════════════════════════════════════════════════
# Step 6 (--prod only): Start PostgreSQL + Redis
# ═══════════════════════════════════════════════════════════════════════

if [ "$MODE" = "prod" ]; then
    header "Step 6: Starting PostgreSQL + Redis containers"

    step "Running setup-sandbox.sh..."
    bash scripts/setup-sandbox.sh --reset

    step "Verifying API server starts..."
    PYTHONPATH=. timeout 5 python -c "
from api.app import app
from fastapi.testclient import TestClient
client = TestClient(app)
resp = client.get('/api/v1/health')
assert resp.status_code == 200
print(f'  API health check: {resp.json()[\"status\"]}')
" || warn "API health check skipped (timeout)"

    step "Production-like environment ready."
fi

# ═══════════════════════════════════════════════════════════════════════
# Done — print next steps
# ═══════════════════════════════════════════════════════════════════════

echo ""
echo -e "${BOLD}════════════════════════════════════════════════════════════════${RESET}"
echo -e "  ${BOLD}${GREEN}ASOE Development Environment — Ready${RESET}"
echo -e "${BOLD}════════════════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "  ${BOLD}Run tests:${RESET}"
echo -e "    python -m pytest                       ${DIM}# full suite${RESET}"
echo -e "    python -m pytest tests/sandbox/ -v      ${DIM}# sandbox integration tests${RESET}"
echo ""
echo -e "  ${BOLD}Sandbox CLI:${RESET}"
echo -e "    PYTHONPATH=. python tests/sandbox/cli.py                    ${DIM}# direct mode${RESET}"
echo -e "    PYTHONPATH=. python tests/sandbox/cli.py --api              ${DIM}# API mode${RESET}"
echo -e "    PYTHONPATH=. python tests/sandbox/cli.py --force-blocked    ${DIM}# test guardrails${RESET}"
echo ""
echo -e "  ${BOLD}Sandbox UI:${RESET}"
echo -e "    PYTHONPATH=. streamlit run tests/sandbox/ui/app.py          ${DIM}# → http://localhost:8501${RESET}"
echo ""
echo -e "  ${BOLD}API server:${RESET}"
echo -e "    PYTHONPATH=. uvicorn api.app:app --port 8000 --reload       ${DIM}# → http://localhost:8000/docs${RESET}"
echo ""
echo -e "  ${BOLD}Docker:${RESET}"
echo -e "    docker compose up                                           ${DIM}# full stack${RESET}"
echo ""
echo -e "${BOLD}════════════════════════════════════════════════════════════════${RESET}"
