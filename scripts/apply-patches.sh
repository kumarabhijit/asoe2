#!/usr/bin/env bash
# apply-patches.sh — apply compatibility patches after uv sync / pip install
# Run once after installing dependencies in a new environment.
#
# Current patches:
#   pydantic 2.12.5 — Python 3.14 rc2 renamed typing._eval_type(prefer_fwd_module)
#   to typing._eval_type(parent_fwdref). Pydantic's 3.14 branch still uses the
#   old name. Patch until pydantic >=2.12.6 ships with the fix.
#   Upstream issue: https://github.com/pydantic/pydantic/issues/<tbd>

set -euo pipefail

PYTHON="${1:-python}"
SITE_PACKAGES=$("$PYTHON" -c "import site; print(site.getsitepackages()[0])")
TARGET="$SITE_PACKAGES/pydantic/_internal/_typing_extra.py"

if [ ! -f "$TARGET" ]; then
    echo "apply-patches.sh: pydantic not found at $TARGET — skipping"
    exit 0
fi

# Only patch if the old (broken) kwarg is still present
if grep -q "prefer_fwd_module=True" "$TARGET"; then
    sed -i 's/prefer_fwd_module=True/parent_fwdref=True/' "$TARGET"
    echo "apply-patches.sh: patched pydantic prefer_fwd_module → parent_fwdref in $TARGET"
else
    echo "apply-patches.sh: pydantic patch not needed (already applied or pydantic updated)"
fi
