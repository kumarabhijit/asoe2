#!/bin/bash
# SessionStart hook — re-export LLM provider secrets into $CLAUDE_ENV_FILE
# so they're available to every tool the harness invokes (uvicorn,
# pytest, python -m main, etc.) without ever appearing in chat or being
# committed to the repo.
#
# How the secret reaches the hook:
#   - Set ANTHROPIC_API_KEY (and friends) in Claude Code on the web's
#     project Settings → Environment variables.
#   - The harness injects them as process env vars when this script runs.
#   - We append `export VAR=...` lines to $CLAUDE_ENV_FILE so the
#     harness's downstream tool invocations inherit them.
#
# Variables forwarded:
#   ANTHROPIC_API_KEY        — required for ASOE_LLM_PROVIDER=anthropic
#   OPENAI_API_KEY           — required for ASOE_LLM_PROVIDER=openai
#   HUGGINGFACE_API_KEY      — required for ASOE_LLM_PROVIDER=huggingface
#   GOOGLE_API_KEY           — required for ASOE_LLM_PROVIDER=google
#   OLLAMA_HOST              — optional override for ollama provider
#   LANGFUSE_PUBLIC_KEY      — optional observability span export
#   LANGFUSE_SECRET_KEY      — optional observability span export
#   LANGFUSE_HOST            — optional langfuse self-host URL
#
# Idempotent: writing the same export twice in $CLAUDE_ENV_FILE is
# harmless — `source`-ing the file just sets the var twice.
set -euo pipefail

if [ -z "${CLAUDE_ENV_FILE:-}" ]; then
  # Local dev fallback — running this script outside Claude Code on the
  # web. Nothing to do; user's local shell already has the env.
  exit 0
fi

forwarded=()
for var in \
  ANTHROPIC_API_KEY \
  OPENAI_API_KEY \
  HUGGINGFACE_API_KEY \
  GOOGLE_API_KEY \
  OLLAMA_HOST \
  LANGFUSE_PUBLIC_KEY \
  LANGFUSE_SECRET_KEY \
  LANGFUSE_HOST; do
  value="${!var:-}"
  if [ -n "$value" ]; then
    # %q quote-escapes the value so secrets containing shell
    # metacharacters survive intact when the env file is sourced.
    printf 'export %s=%q\n' "$var" "$value" >> "$CLAUDE_ENV_FILE"
    forwarded+=("$var")
  fi
done

# Stderr only — keep stdout clean for the hook protocol.
if [ ${#forwarded[@]} -gt 0 ]; then
  echo "[session-start] forwarded to \$CLAUDE_ENV_FILE: ${forwarded[*]}" >&2
else
  echo "[session-start] no LLM secrets present in the session env — nothing forwarded" >&2
fi
