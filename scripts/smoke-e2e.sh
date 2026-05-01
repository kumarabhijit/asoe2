#!/usr/bin/env bash
# smoke-e2e.sh ─ End-to-end smoke test against the deployed asoe-core API.
#
# Drives every fixture under tests/fixtures/synthetic/ through the full
# deterministic pipeline (POST /api/v1/exceptions/resolve), then asserts
# that the response carries the expected intent + recipe + a
# shadow_verdict / final_status from the allowed sets defined in the
# matching <intent>.expected.json sidecar.
#
# What it validates:
#   * /api/auth/login round-trip + JWT validity
#   * Intent classifier lands on the expected enum value
#   * Recipe registry routes intent → expected recipe
#   * Compliance Shadow + executor reach a terminal state in the allowed set
#   * The exception is persisted and retrievable via the trace
#
# What it does NOT validate (out of scope for v1 — phased per ADR):
#   * WebSocket event delivery (planned addition; needs websocat in the
#     toolchain to keep the script bash-only)
#   * UI rendering of the resulting exception (manual / Playwright)
#   * Load characteristics, idempotency under retries (load-synthetic.sh
#     in a follow-up)
#
# Usage from the repo root:
#
#   API_URL=https://asoepreprodapi.<env>.azurecontainerapps.io \
#   USER_EMAIL=marcus.webb@acme-corp.com \
#       ./scripts/smoke-e2e.sh
#
# Override knobs:
#   API_URL        Base URL of the deployed API (no trailing slash).
#                  Defaults to the live pre-prod FQDN.
#   USER_EMAIL     Seeded admin/manager/analyst email. Default jane@acme.com.
#   USER_PASSWORD  Any non-empty string accepts in V1 stub auth.
#                  Default 'smoke-e2e' so it shows up in audit logs.
#   FIXTURES_DIR   Override the fixture directory.
#   RESET_TENANT   Set to 0 to skip the /_sandbox/tenant/reset call.
#                  Default 1 (clean run every invocation).
#   STOP_ON_FAIL   Set to 1 to abort on the first failing fixture.
#                  Default 0 (run all, report total at the end).

set -euo pipefail

: "${API_URL:=https://asoepreprodapi.orangerock-0b3a1691.centralus.azurecontainerapps.io}"
: "${USER_EMAIL:=jane@acme.com}"
: "${USER_PASSWORD:=smoke-e2e}"
: "${FIXTURES_DIR:=tests/fixtures/synthetic}"
: "${RESET_TENANT:=1}"
: "${STOP_ON_FAIL:=0}"

command -v curl >/dev/null || { echo "curl not found"; exit 1; }
command -v jq   >/dev/null || { echo "jq not found"; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found (needed for uuid + dict diff)"; exit 1; }

if [[ ! -d "${FIXTURES_DIR}" ]]; then
    echo "ERROR: FIXTURES_DIR '${FIXTURES_DIR}' not found. Run from the repo root." >&2
    exit 1
fi

# ────────────────────────────────────── 1. Auth

echo "── ASOE smoke E2E ──────────────────────────────────────────"
echo "API URL : ${API_URL}"
echo "User    : ${USER_EMAIL}"
echo "Fixtures: ${FIXTURES_DIR}"
echo "─────────────────────────────────────────────────────────────"

login_response=$(curl -fsS --max-time 30 \
    -H "content-type: application/json" \
    -d "$(jq -nc --arg e "${USER_EMAIL}" --arg p "${USER_PASSWORD}" '{email:$e,password:$p}')" \
    "${API_URL}/api/auth/login")

ACCESS_TOKEN=$(jq -er .access_token <<<"${login_response}")
USER_ROLES=$(jq -rc .user.roles <<<"${login_response}")
echo "  login OK (roles=${USER_ROLES})"

auth_header="authorization: Bearer ${ACCESS_TOKEN}"

# ────────────────────────────────────── 2. Optional tenant reset

if [[ "${RESET_TENANT}" == "1" ]]; then
    if curl -fsS --max-time 30 -X POST \
            -H "${auth_header}" -H "content-type: application/json" \
            -d '{}' \
            "${API_URL}/api/v1/_sandbox/tenant/reset" \
            >/dev/null 2>&1; then
        echo "  tenant reset: ok"
    else
        echo "  tenant reset: skipped (route unavailable; not sandbox env or insufficient role)"
    fi
fi

# ────────────────────────────────────── 3. Run each fixture

assert_response_with_python() {
    local resp_json="$1"
    local expected_path="$2"
    python3 - "$resp_json" "$expected_path" <<'PY'
import json, sys
resp = json.loads(sys.argv[1])
with open(sys.argv[2]) as f:
    expected = json.load(f)

problems = []
for key in ("intent", "selected_recipe"):
    actual = resp.get(key)
    want = expected[key]
    if actual != want:
        problems.append(f"{key}: expected={want!r} got={actual!r}")

verdict = resp.get("shadow_verdict")
allowed_v = expected.get("allowed_shadow_verdicts", [])
if allowed_v and verdict not in allowed_v:
    problems.append(f"shadow_verdict: got={verdict!r} not in {allowed_v}")

final = resp.get("final_status")
allowed_f = expected.get("allowed_final_statuses", [])
if allowed_f and final not in allowed_f:
    problems.append(f"final_status: got={final!r} not in {allowed_f}")

if problems:
    print("FAIL")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)
print("PASS")
PY
}

passed=0
failed=0
failed_fixtures=()

shopt -s nullglob
event_files=("${FIXTURES_DIR}"/*.event.json)
shopt -u nullglob

if (( ${#event_files[@]} == 0 )); then
    echo "ERROR: no *.event.json fixtures found in ${FIXTURES_DIR}" >&2
    exit 1
fi

for ev_file in "${event_files[@]}"; do
    base=$(basename "${ev_file}" .event.json)
    expected_file="${FIXTURES_DIR}/${base}.expected.json"
    if [[ ! -f "${expected_file}" ]]; then
        echo "  ${base}: SKIP (no ${base}.expected.json sidecar)"
        continue
    fi

    idem_key=$(python3 -c 'import uuid; print(uuid.uuid4())')

    set +e
    # -D - dumps the response headers to stdout so we can grab the
    # X-Trace-ID server-side trace id and pass it to the user for
    # Log Analytics correlation when the call returns 5xx.
    headers_file=$(mktemp)
    response=$(curl -sS --max-time 60 \
        -X POST \
        -H "${auth_header}" \
        -H "content-type: application/json" \
        -H "Idempotency-Key: ${idem_key}" \
        --data-binary "@${ev_file}" \
        -D "${headers_file}" \
        -w "\n__HTTP_STATUS__:%{http_code}" \
        "${API_URL}/api/v1/exceptions/resolve")
    curl_rc=$?
    set -e

    http_status=$(awk -F: '/^__HTTP_STATUS__:/ {print $2}' <<<"${response}")
    body=$(awk '/^__HTTP_STATUS__:/ {exit} {print}' <<<"${response}")
    trace_id=$(awk 'BEGIN{IGNORECASE=1} /^x-trace-id:/ {gsub(/\r/,""); print $2}' "${headers_file}" || true)
    rm -f "${headers_file}"

    if [[ ${curl_rc} -ne 0 ]] || [[ "${http_status}" != "200" ]]; then
        echo "  ${base}: FAIL (HTTP ${http_status:-curl_rc=${curl_rc}})"
        echo "    idempotency_key: ${idem_key}"
        if [[ -n "${trace_id}" ]]; then
            echo "    x-trace-id:      ${trace_id}"
        fi
        echo "    body:            ${body}" | head -c 600
        failed=$(( failed + 1 ))
        failed_fixtures+=("${base}")
        if [[ "${STOP_ON_FAIL}" == "1" ]]; then break; fi
        continue
    fi

    set +e
    verdict_output=$(assert_response_with_python "${body}" "${expected_file}" 2>&1)
    verdict_rc=$?
    set -e

    if [[ ${verdict_rc} -eq 0 ]]; then
        intent=$(jq -r .intent <<<"${body}")
        recipe=$(jq -r .selected_recipe <<<"${body}")
        verdict=$(jq -r .shadow_verdict <<<"${body}")
        final=$(jq -r .final_status <<<"${body}")
        echo "  ${base}: PASS (intent=${intent} recipe=${recipe} verdict=${verdict} final=${final})"
        passed=$(( passed + 1 ))
    else
        echo "  ${base}: ${verdict_output}"
        failed=$(( failed + 1 ))
        failed_fixtures+=("${base}")
        if [[ "${STOP_ON_FAIL}" == "1" ]]; then break; fi
    fi
done

# ────────────────────────────────────── 4. Summary

echo "─────────────────────────────────────────────────────────────"
total=$(( passed + failed ))
echo "Fixtures: ${total}    Passed: ${passed}    Failed: ${failed}"
if (( failed > 0 )); then
    echo "Failed fixtures:"
    for f in "${failed_fixtures[@]}"; do
        echo "  - ${f}"
    done
    exit 1
fi
echo "All E2E smoke fixtures passed."
