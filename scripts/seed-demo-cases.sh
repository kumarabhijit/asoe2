#!/usr/bin/env bash
# seed-demo-cases.sh ─ Populate a deployed asoe-core API with a
# representational catalog of cases for the All Cases surface.
#
# WHY THIS EXISTS
# ---------------
# The asoe-ui Vercel/local deployment renders a rich, hand-authored mock
# catalog (asoe-ui/src/lib/mock-data/) — every intent / supergroup /
# verdict on screen. The Azure pre-prod deployment runs the REAL backend
# (NEXT_PUBLIC_USE_REAL_API=1) against PostgreSQL, so its All Cases page
# is only as full as the data that has actually been ingested. On a fresh
# deploy that's empty, which makes Azure look broken next to the Vercel
# demo.
#
# This script closes that gap the architecture-honest way: it drives
# synthetic events through the SAME deterministic ingestion path the
# product uses (POST /api/v1/exceptions/resolve) plus the sandbox
# email-intake producer, so the resulting cases, classifications, shadow
# verdicts and analyses are all produced by the real Skill→Shadow→Recipe
# pipeline — never fabricated and inserted behind it. (Raw SQL inserts
# into `order_case` would NOT work: GET /api/v1/cases reads the in-memory
# CaseStore, which is only populated by a pipeline run in-process.)
#
# ⚠ DURABILITY / MULTI-REPLICA CAVEAT (read before relying on this)
# -----------------------------------------------------------------
# OrderCases are NOT persisted to Postgres yet — the DB-backed CaseStore
# is deferred to "Phase H.7" (api/store.py:1429). The in-memory CaseStore
# is per-process, per-replica, and is wiped on every container restart,
# deploy, crash, or scale-in. Implications for this seeder:
#   * Seeded cases DO NOT survive a restart — re-run after each one.
#   * Azure runs minReplicas=1/maxReplicas=2; each replica has its OWN
#     CaseStore. The UI reads round-robin, so a page served by the
#     replica that didn't receive the seed shows an empty/partial list.
#     For a COHERENT demo, pin maxReplicas=1 for the demo window
#     (infra/parameters.sandbox.json) before seeding.
# The right fix is to land Phase H.7 (DB-backed cases); this seeder is a
# stopgap on top of that gap, not a substitute for it.
#
# WHAT IT SEEDS (mirrors the mock supergroup / verdict spread)
#   * tests/fixtures/synthetic/*.event.json  — 10 deterministic intents:
#       CONTRACTUAL_CORRECTION, CREDIT_BLOCK, DUPLICATE_PO, EDI_MISMATCH,
#       BACK_ORDER, OVER_MAX, MIN_ORDER_QTY, PALLET_CONFIG,
#       DELIVERY_DELAY, PRICE_HOLD_RELEASE   (GREEN / YELLOW / RED spread)
#   * scripts/seed-fixtures/*.event.json     — demo-only extras:
#       MASS_PRICING_ERROR → FAIL_TO_HUMAN (the FAILED-case representation)
#   * /api/v1/_sandbox/seed/manual-order-intake — SG_NEW_ORDER email cases
#       at two confidence bands (GREEN auto + YELLOW STANDARD_REVIEW)
#
# This is a SEEDER, not a test: it never asserts expected outcomes and a
# single bad fixture does not fail the run (it's reported and skipped).
# For the pass/fail contract check use scripts/smoke-e2e.sh instead.
#
# Usage from the repo root:
#
#   API_URL=https://asoepreprodapi.<env>.azurecontainerapps.io \
#   USER_EMAIL=jane@acme.com \
#       ./scripts/seed-demo-cases.sh
#
# Override knobs:
#   API_URL          Base URL of the deployed API (no trailing slash).
#                    Defaults to the live pre-prod FQDN.
#   USER_EMAIL       Seeded admin/manager email (reset + sandbox routes
#                    need manager/admin). Default jane@acme.com.
#   USER_PASSWORD    Any non-empty string in V1 stub auth. Default
#                    'seed-demo-cases' so it shows up in audit logs.
#   RESET_TENANT     0 (default). Set 1 to attempt /_sandbox/tenant/reset
#                    first. NOTE: that route only clears the in-memory
#                    ExceptionStore's _records; against the DB-backed
#                    store it is a NO-OP (no _records attr) and it never
#                    clears the CaseStore at all (api/routes/sandbox.py).
#                    So it does NOT make re-runs idempotent on Azure —
#                    each re-run accumulates duplicate exceptions in
#                    Postgres. Prefer redeploy (fresh container = empty
#                    in-memory cases) over relying on reset.
#   SEED_SYNTHETIC   1 (default) ingest tests/fixtures/synthetic.
#   SEED_DEMO_EXTRA  1 (default) ingest scripts/seed-fixtures.
#   SEED_MANUAL_INTAKE 1 (default) seed the email order-intake cases.
#   SYNTHETIC_DIR    Override the synthetic fixture directory.
#   DEMO_DIR         Override the demo-only fixture directory.

set -euo pipefail

: "${API_URL:=https://asoepreprodapi.orangerock-0b3a1691.centralus.azurecontainerapps.io}"
: "${USER_EMAIL:=jane@acme.com}"
: "${USER_PASSWORD:=seed-demo-cases}"
: "${RESET_TENANT:=0}"
: "${SEED_SYNTHETIC:=1}"
: "${SEED_DEMO_EXTRA:=1}"
: "${SEED_MANUAL_INTAKE:=1}"
: "${SYNTHETIC_DIR:=tests/fixtures/synthetic}"
: "${DEMO_DIR:=scripts/seed-fixtures}"

command -v curl >/dev/null || { echo "curl not found"; exit 1; }
command -v jq   >/dev/null || { echo "jq not found"; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found (needed for uuid)"; exit 1; }

echo "── ASOE seed demo cases ────────────────────────────────────"
echo "API URL : ${API_URL}"
echo "User    : ${USER_EMAIL}"
echo "Reset   : ${RESET_TENANT}"
echo "─────────────────────────────────────────────────────────────"

# ────────────────────────────────────── 1. Auth

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
        echo "  tenant reset: route accepted (NOTE: clears in-memory"
        echo "                exception _records only — NO-OP on the"
        echo "                DB-backed store; never clears cases)"
    else
        echo "  tenant reset: skipped (route unavailable; not sandbox env or insufficient role)"
    fi
fi

seeded=0
errors=0
error_items=()

# ────────────────────────────────────── 3. Ingest an event fixture
# Drives one *.event.json through the real pipeline and reports the
# resulting classification — never asserts, so a single bad fixture is
# logged and skipped rather than aborting the whole seed.

ingest_event() {
    local ev_file="$1"
    local base
    base=$(basename "${ev_file}" .event.json)
    local idem_key
    idem_key=$(python3 -c 'import uuid; print(uuid.uuid4())')

    set +e
    local response http_status body
    response=$(curl -sS --max-time 60 \
        -X POST \
        -H "${auth_header}" \
        -H "content-type: application/json" \
        -H "Idempotency-Key: ${idem_key}" \
        --data-binary "@${ev_file}" \
        -w "\n__HTTP_STATUS__:%{http_code}" \
        "${API_URL}/api/v1/exceptions/resolve")
    local curl_rc=$?
    set -e

    http_status=$(awk -F: '/^__HTTP_STATUS__:/ {print $2}' <<<"${response}")
    body=$(awk '/^__HTTP_STATUS__:/ {exit} {print}' <<<"${response}")

    if [[ ${curl_rc} -ne 0 ]] || [[ "${http_status}" != "200" ]]; then
        echo "  ${base}: ERROR (HTTP ${http_status:-curl_rc=${curl_rc}})"
        echo "    body: $(head -c 300 <<<"${body}")"
        errors=$(( errors + 1 ))
        error_items+=("${base}")
        return
    fi

    local intent recipe verdict final
    intent=$(jq -r '.intent // "—"' <<<"${body}")
    recipe=$(jq -r '.selected_recipe // "—"' <<<"${body}")
    verdict=$(jq -r '.shadow_verdict // "—"' <<<"${body}")
    final=$(jq -r '.final_status // "—"' <<<"${body}")
    echo "  ${base}: ok (intent=${intent} recipe=${recipe} verdict=${verdict} final=${final})"
    seeded=$(( seeded + 1 ))
}

ingest_dir() {
    local dir="$1"
    if [[ ! -d "${dir}" ]]; then
        echo "  (skip) ${dir}: not found — run from the repo root"
        return
    fi
    shopt -s nullglob
    local files=("${dir}"/*.event.json)
    shopt -u nullglob
    if (( ${#files[@]} == 0 )); then
        echo "  (skip) ${dir}: no *.event.json fixtures"
        return
    fi
    for ev_file in "${files[@]}"; do
        ingest_event "${ev_file}"
    done
}

if [[ "${SEED_SYNTHETIC}" == "1" ]]; then
    echo "── synthetic intents (${SYNTHETIC_DIR}) ──"
    ingest_dir "${SYNTHETIC_DIR}"
fi

if [[ "${SEED_DEMO_EXTRA}" == "1" ]]; then
    echo "── demo-only extras (${DEMO_DIR}) ──"
    ingest_dir "${DEMO_DIR}"
fi

# ────────────────────────────────────── 4. Email order-intake cases
# The producer endpoint emits a MANUAL_ORDER_INTAKE event through the
# normal resolve graph. composite_confidence drives the routing band:
#   0.97 → clean GREEN auto-intake
#   0.88 → STANDARD_REVIEW (YELLOW), needs human — mirrors mock exc-026
# Mirrors the SG_NEW_ORDER email cases in the Vercel mock.

seed_manual_intake() {
    local order_id="$1"
    local confidence="$2"
    local label="$3"
    set +e
    local response http_status body
    response=$(curl -sS --max-time 60 \
        -X POST \
        -H "${auth_header}" \
        -H "content-type: application/json" \
        -d "$(jq -nc --arg o "${order_id}" --argjson c "${confidence}" \
              '{order_id:$o, composite_confidence:$c}')" \
        -w "\n__HTTP_STATUS__:%{http_code}" \
        "${API_URL}/api/v1/_sandbox/seed/manual-order-intake")
    local curl_rc=$?
    set -e
    http_status=$(awk -F: '/^__HTTP_STATUS__:/ {print $2}' <<<"${response}")
    body=$(awk '/^__HTTP_STATUS__:/ {exit} {print}' <<<"${response}")
    if [[ ${curl_rc} -ne 0 ]] || [[ "${http_status}" != "200" ]]; then
        echo "  manual-intake ${label}: ERROR (HTTP ${http_status:-curl_rc=${curl_rc}})"
        echo "    body: $(head -c 300 <<<"${body}")"
        errors=$(( errors + 1 ))
        error_items+=("manual-intake:${label}")
        return
    fi
    local exc final
    exc=$(jq -r '.exception_id // "—"' <<<"${body}")
    final=$(jq -r '.final_status // "—"' <<<"${body}")
    echo "  manual-intake ${label}: ok (exception_id=${exc} final=${final})"
    seeded=$(( seeded + 1 ))
}

# B3 — customer-email supergroup variety (ADR-036). The manual-intake
# producer is the labelled stand-in for the email-intelligence-agent;
# passing an `email_supergroup_hint` on metadata drives the Phase-1
# deterministic supergroup classifier (skills/email_supergroup_classifier),
# so the case opens under the given CUSTOMER supergroup with a MODEL
# classification — the same variety the asoe-ui mock fabricates, now on
# the real path. The hint rides metadata_extra (a free-form passthrough);
# the endpoint is unchanged.
seed_email_supergroup() {
    local order_id="$1"
    local supergroup="$2"
    set +e
    local response http_status body
    response=$(curl -sS --max-time 60 \
        -X POST \
        -H "${auth_header}" \
        -H "content-type: application/json" \
        -d "$(jq -nc --arg o "${order_id}" --arg sg "${supergroup}" \
              '{order_id:$o, composite_confidence:0.9, metadata_extra:{email_supergroup_hint:$sg}}')" \
        -w "\n__HTTP_STATUS__:%{http_code}" \
        "${API_URL}/api/v1/_sandbox/seed/manual-order-intake")
    local curl_rc=$?
    set -e
    http_status=$(awk -F: '/^__HTTP_STATUS__:/ {print $2}' <<<"${response}")
    body=$(awk '/^__HTTP_STATUS__:/ {exit} {print}' <<<"${response}")
    if [[ ${curl_rc} -ne 0 ]] || [[ "${http_status}" != "200" ]]; then
        echo "  email-supergroup ${supergroup}: ERROR (HTTP ${http_status:-curl_rc=${curl_rc}})"
        echo "    body: $(head -c 300 <<<"${body}")"
        errors=$(( errors + 1 ))
        error_items+=("email-supergroup:${supergroup}")
        return
    fi
    local exc
    exc=$(jq -r '.exception_id // "—"' <<<"${body}")
    echo "  email-supergroup ${supergroup}: ok (exception_id=${exc})"
    seeded=$(( seeded + 1 ))
}

if [[ "${SEED_MANUAL_INTAKE}" == "1" ]]; then
    echo "── email order-intake (SG_NEW_ORDER) ──"
    seed_manual_intake "EML-SEED-GREEN-001"  0.97 "GREEN(auto)"
    seed_manual_intake "EML-SEED-REVIEW-001" 0.88 "YELLOW(review)"

    # B4 — multi-record case. Two intake events sharing one PO
    # (order_id == customer_po in V1) materialise onto the SAME case, so
    # the demo exercises the case roll-up + record-picker surface
    # (/cases/[id]?record=<id>) on real data instead of only single-
    # record cases. The case status rolls up to its least-settled child.
    seed_manual_intake "EML-SEED-MULTI-001"  0.95 "multi-record #1"
    seed_manual_intake "EML-SEED-MULTI-001"  0.83 "multi-record #2 (same PO)"

    # B3 — customer-email supergroup variety (ADR-036, Phase 1). Drives
    # the deterministic supergroup classifier so the Customer Inbox shows
    # the same variety on Azure that the asoe-ui mock fabricates for the
    # preview. These are MODEL classifications on the real path — the
    # honest replacement for the previously-faked variety. When the live
    # email-intelligence-agent (ADR-036 Phase 3) lands, the hint is
    # replaced by real classification; the seed calls stay valid.
    echo "── customer-email supergroups (B3 / ADR-036) ──"
    seed_email_supergroup "EML-SG-CHANGE-001"  "SG_ORDER_CHANGE"
    seed_email_supergroup "EML-SG-INQUIRY-001" "SG_ORDER_STATUS_INQUIRY"
    seed_email_supergroup "EML-SG-SHIP-001"    "SG_SHIPMENT_DISCREPANCY"
    seed_email_supergroup "EML-SG-COMPLAINT-001" "SG_COMPLAINT_SERVICE"
    seed_email_supergroup "EML-SG-DOC-001"     "SG_DOCUMENTATION"
    seed_email_supergroup "EML-SG-BILLING-001" "SG_BILLING_DISPUTE"
fi

# B5 — draft replies render via the analysis composer
# (compose_draft_reply, api/routes/exceptions.py) for every record; the
# seeded intake cases above exercise that surface. No separate seed step.

# ────────────────────────────────────── 5. Summary

echo "─────────────────────────────────────────────────────────────"
echo "Seeded: ${seeded}    Errors: ${errors}"
if (( errors > 0 )); then
    echo "Items with errors:"
    for it in "${error_items[@]}"; do
        echo "  - ${it}"
    done
    exit 1
fi
echo "All demo cases seeded. Open the All Cases page to verify."
