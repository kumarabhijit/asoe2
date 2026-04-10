#!/usr/bin/env bash
# setup-sandbox.sh — Initialize the ASOE sandbox environment.
#
# Sets up a test-containerized PostgreSQL instance, applies DB migrations,
# seeds sandbox data, and verifies service readiness.
#
# Architecture_v3.md Section 4.4:
#   postgres  — PostgreSQL 16 + pgvector
#   redis     — Redis 7+ (pub/sub, task queue, session cache)
#
# Usage:
#   scripts/setup-sandbox.sh              # full setup
#   scripts/setup-sandbox.sh --db-only    # database only (skip Redis)
#   scripts/setup-sandbox.sh --reset      # tear down and recreate
#   scripts/setup-sandbox.sh --ci         # CI mode (SQLite, no containers)
#
# Environment variables:
#   DATABASE_URL          PostgreSQL connection string (default: sqlite for CI)
#   REDIS_URL             Redis connection string (default: unset = in-memory)
#   SANDBOX_DB_PATH       SQLite path for CI mode (default: tests/sandbox/sandbox.db)
#   ASOE_ENV              Environment name (default: sandbox)
#   ASOE_JWT_SECRET       JWT signing secret (default: dev secret)
#   PG_CONTAINER_NAME     PostgreSQL container name (default: asoe-sandbox-pg)
#   REDIS_CONTAINER_NAME  Redis container name (default: asoe-sandbox-redis)

set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PG_CONTAINER_NAME="${PG_CONTAINER_NAME:-asoe-sandbox-pg}"
REDIS_CONTAINER_NAME="${REDIS_CONTAINER_NAME:-asoe-sandbox-redis}"
PG_PORT="${PG_PORT:-5433}"
REDIS_PORT="${REDIS_PORT:-6380}"
PG_USER="${PG_USER:-asoe_sandbox}"
PG_PASSWORD="${PG_PASSWORD:-sandbox_secret}"
PG_DB="${PG_DB:-asoe_sandbox}"

export ASOE_ENV="${ASOE_ENV:-sandbox}"
export ASOE_JWT_SECRET="${ASOE_JWT_SECRET:-asoe-dev-secret-do-not-use-in-production}"

RESET=false
DB_ONLY=false
CI_MODE=false

for arg in "$@"; do
    case "$arg" in
        --reset)    RESET=true ;;
        --db-only)  DB_ONLY=true ;;
        --ci)       CI_MODE=true ;;
        *)          echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

# ── Color helpers ───────────────────────────────────────────────────────────

GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
BOLD='\033[1m'
RESET_COLOR='\033[0m'

info()  { echo -e "${GREEN}[sandbox]${RESET_COLOR} $*"; }
warn()  { echo -e "${YELLOW}[sandbox]${RESET_COLOR} $*"; }
error() { echo -e "${RED}[sandbox]${RESET_COLOR} $*"; }

# ── CI mode (SQLite, no containers) ─────────────────────────────────────────

if $CI_MODE; then
    info "CI mode — using SQLite, no containers"
    SANDBOX_DB_PATH="${SANDBOX_DB_PATH:-$PROJECT_ROOT/tests/sandbox/sandbox.db}"
    export DATABASE_URL="sqlite:///$SANDBOX_DB_PATH"

    if $RESET && [ -f "$SANDBOX_DB_PATH" ]; then
        info "Removing existing sandbox DB: $SANDBOX_DB_PATH"
        rm -f "$SANDBOX_DB_PATH"
    fi

    info "Applying SQLite migrations..."
    cd "$PROJECT_ROOT"
    PYTHONPATH=. python -c "
from db.migrations.runner import apply_migrations
apply_migrations('$DATABASE_URL')
print('SQLite schema applied.')
"

    info "Seeding sandbox data..."
    PYTHONPATH=. python tests/sandbox/seed.py --db "$SANDBOX_DB_PATH"

    info "CI sandbox ready."
    info "  DATABASE_URL=$DATABASE_URL"
    info "  ASOE_ENV=$ASOE_ENV"
    exit 0
fi

# ── Tear down (--reset) ────────────────────────────────────────────────────

if $RESET; then
    warn "Tearing down existing sandbox containers..."
    docker rm -f "$PG_CONTAINER_NAME" 2>/dev/null || true
    if ! $DB_ONLY; then
        docker rm -f "$REDIS_CONTAINER_NAME" 2>/dev/null || true
    fi
    info "Containers removed."
fi

# ── PostgreSQL container ────────────────────────────────────────────────────

if docker ps -a --format '{{.Names}}' | grep -q "^${PG_CONTAINER_NAME}$"; then
    if docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER_NAME}$"; then
        info "PostgreSQL container '$PG_CONTAINER_NAME' already running."
    else
        info "Starting existing PostgreSQL container..."
        docker start "$PG_CONTAINER_NAME"
    fi
else
    info "Creating PostgreSQL 16 container..."
    docker run -d \
        --name "$PG_CONTAINER_NAME" \
        -e POSTGRES_USER="$PG_USER" \
        -e POSTGRES_PASSWORD="$PG_PASSWORD" \
        -e POSTGRES_DB="$PG_DB" \
        -p "${PG_PORT}:5432" \
        postgres:16-alpine

    info "Waiting for PostgreSQL to accept connections..."
    for i in $(seq 1 30); do
        if docker exec "$PG_CONTAINER_NAME" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
            info "PostgreSQL ready after ${i}s."
            break
        fi
        if [ "$i" -eq 30 ]; then
            error "PostgreSQL did not become ready within 30 seconds."
            exit 1
        fi
        sleep 1
    done
fi

export DATABASE_URL="postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}"

# ── Redis container ─────────────────────────────────────────────────────────

if ! $DB_ONLY; then
    if docker ps -a --format '{{.Names}}' | grep -q "^${REDIS_CONTAINER_NAME}$"; then
        if docker ps --format '{{.Names}}' | grep -q "^${REDIS_CONTAINER_NAME}$"; then
            info "Redis container '$REDIS_CONTAINER_NAME' already running."
        else
            info "Starting existing Redis container..."
            docker start "$REDIS_CONTAINER_NAME"
        fi
    else
        info "Creating Redis 7 container..."
        docker run -d \
            --name "$REDIS_CONTAINER_NAME" \
            -p "${REDIS_PORT}:6379" \
            redis:7-alpine

        info "Waiting for Redis to accept connections..."
        for i in $(seq 1 15); do
            if docker exec "$REDIS_CONTAINER_NAME" redis-cli ping 2>/dev/null | grep -q PONG; then
                info "Redis ready after ${i}s."
                break
            fi
            if [ "$i" -eq 15 ]; then
                error "Redis did not become ready within 15 seconds."
                exit 1
            fi
            sleep 1
        done
    fi

    export REDIS_URL="redis://localhost:${REDIS_PORT}/0"
fi

# ── Apply DB migrations ────────────────────────────────────────────────────

info "Applying database migrations..."
cd "$PROJECT_ROOT"
PYTHONPATH=. python -c "
from db.migrations.runner import apply_migrations
apply_migrations('$DATABASE_URL')
print('Migrations applied successfully.')
"

# ── Seed sandbox data ──────────────────────────────────────────────────────

info "Seeding sandbox data..."
PYTHONPATH=. python tests/sandbox/seed.py --db "$PROJECT_ROOT/tests/sandbox/sandbox.db"

# ── Summary ─────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}════════════════════════════════════════════════════════════════${RESET_COLOR}"
echo -e "  ${BOLD}ASOE Sandbox — Ready${RESET_COLOR}"
echo -e "${BOLD}════════════════════════════════════════════════════════════════${RESET_COLOR}"
echo -e "  DATABASE_URL:   $DATABASE_URL"
if [ -n "${REDIS_URL:-}" ]; then
echo -e "  REDIS_URL:      $REDIS_URL"
fi
echo -e "  ASOE_ENV:       $ASOE_ENV"
echo -e "  ASOE_JWT_SECRET: (set)"
echo ""
echo -e "  ${GREEN}Run tests:${RESET_COLOR}"
echo -e "    PYTHONPATH=. python -m pytest tests/sandbox/"
echo ""
echo -e "  ${GREEN}Run sandbox CLI:${RESET_COLOR}"
echo -e "    PYTHONPATH=. python tests/sandbox/cli.py"
echo ""
echo -e "  ${GREEN}Start API server:${RESET_COLOR}"
echo -e "    uvicorn api.app:app --port 8000"
echo -e "${BOLD}════════════════════════════════════════════════════════════════${RESET_COLOR}"
