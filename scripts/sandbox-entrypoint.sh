#!/usr/bin/env bash
# sandbox-entrypoint.sh — auto-seed the sandbox SQLite DB on first start,
# then exec the CMD (Streamlit by default).
set -euo pipefail

DB_PATH="${SANDBOX_DB_PATH:-/app/tests/sandbox/sandbox.db}"

if [ ! -f "$DB_PATH" ]; then
    echo "sandbox-entrypoint: seeding sandbox database at $DB_PATH"
    python tests/sandbox/seed.py --db "$DB_PATH"
else
    echo "sandbox-entrypoint: sandbox database already exists at $DB_PATH"
fi

exec "$@"
