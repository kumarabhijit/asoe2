# Runbook — ADR-039 X.2 → X.1 rollback

**Owner:** SRE.
**Audience:** On-call SRE engineer + Compliance escalation contact.
**Trigger:** Any of the SLI alerts in §2 fire after the X.2 / X.3 ratification flip lands `financial_impact_threshold_usd` to a non-null value.
**Outcome:** L2 LLM Shadow returns to observe-only X.1 within ≤5 minutes; deterministic Compliance Shadow remains the single verdict authority. No code redeploy required.

---

## 1. Pre-conditions

* X.2 (or X.3) is currently active. Confirm:
  ```
  kubectl -n asoe get configmap asoe-shadow-llm-bundle -o jsonpath='{.data.metadata\.yaml}' \
    | grep financial_impact_threshold_usd
  ```
  Expected non-null: `10000` (X.2) or `500` (X.3).
* You have `kubectl edit configmap` rights in the `asoe` namespace.
* You can post in the `#asoe-incidents` Slack channel.

If the threshold already shows `null`, you are already on X.1 — investigate the alert separately; rollback is unnecessary.

---

## 2. Triggering SLI alerts

Any one of these is sufficient to roll back. Sustained breach (≥30 min) is mandatory rollback; transient breach is judgement call (consult Compliance contact for borderline).

| Alert | Source metric | Threshold | Severity |
|---|---|---|---|
| `ShadowLLMFalseDowngradeRate` | `shadow_llm_disagreement_rate` × `reviewer_override_rate_on_llm_downgrades` (Grafana derived panel) | >40% sustained 30 min | **mandatory** |
| `ShadowLLMValidationErrorSpike` | `rate(shadow_llm_validation_errors_total[5m])` | >0.5% of invocation rate sustained 5 min | **mandatory** |
| `ReviewerQueueDepth` | `reviewer_queue_depth_p95` (existing exception-queue SLI) | >2× pre-X.2 baseline sustained 30 min | **mandatory** |
| `ShadowLLMUnavailability` | `rate(shadow_llm_unavailable_total[5m])` | >2% of invocation rate sustained 10 min | **mandatory** |
| `ShadowLLMTimeouts` | `rate(shadow_llm_timeouts_total[5m])` | >2% of invocation rate sustained 10 min | discretionary |
| `ComplianceVerdictMismatch` | reviewer-side audit query | >1 case in audit-replay where the post-rollback deterministic verdict ≠ the live combined verdict | **mandatory + escalate** |

The `ComplianceVerdictMismatch` alert is the regression signal — the X.2 combiner produced a verdict the deterministic gate would not have. Escalation path: page Compliance Veto Holder before rollback.

---

## 3. Rollback procedure (≤5 minutes)

### 3.1 Stop the bleeding (two ConfigMap edits)

```bash
# Step A — flip the threshold to null in the bundle ConfigMap.
# This is the canonical X.2 → X.1 reversal.
kubectl -n asoe edit configmap asoe-shadow-llm-bundle
# Find: financial_impact_threshold_usd: 10000  (or 500 on X.3)
# Change to: financial_impact_threshold_usd: null
# Save + close.
```

The combiner reads the threshold from the bundle, which the running pods load at startup AND on `SIGHUP` (per ADR-039 §6.5 rollback policy). Choose ONE of the two options below for propagation.

#### 3.1.A — SIGHUP propagation (preferred; zero downtime)

```bash
# Send SIGHUP to every asoe-core pod so the bundle reloads in
# place. No pod restart; in-flight requests complete normally.
for pod in $(kubectl -n asoe get pods -l app=asoe-core -o name); do
  kubectl -n asoe exec $pod -- kill -HUP 1
done
```

Confirm propagation:

```bash
# The Prometheus counter should show no new YELLOW verdicts
# arriving from the combiner within 60 seconds.
curl -s http://asoe-core.asoe.svc:8000/api/v1/metrics \
  | grep 'shadow_llm_verdicts_total{action="DISAGREE_DOWNGRADE"}'
```

Save the value. Wait 90 seconds. Re-query. The counter should NOT increment for traffic flowing through the combiner. (It can still increment for ABSTAIN / AGREE — those are observe-only and irrelevant to the rollback.)

#### 3.1.B — Rolling restart (fallback when SIGHUP doesn't propagate)

```bash
kubectl -n asoe rollout restart deployment/asoe-core
kubectl -n asoe rollout status deployment/asoe-core --timeout=180s
```

Rolling restart drains old pods over ~60 seconds. Slightly slower than SIGHUP but unconditional.

### 3.2 Disable case-agent routing (optional; only if the alert correlates with the agent path)

If `ShadowLLMValidationErrorSpike` is the trigger AND the spike correlates with `EMAIL_ORDER_ENTRY_REQUEST` events (per `kubectl logs ... | grep MANUAL_ORDER_INTAKE`), also flip the routing predicate off:

```bash
kubectl -n asoe edit configmap asoe-core-config
# Set: ASOE_CASE_AGENT_ENABLED: "0"
```

Then SIGHUP / restart per §3.1.A or §3.1.B. The deterministic graph picks up MANUAL_ORDER_INTAKE events again.

### 3.3 Confirm fall-through behaviour

```bash
# Validation errors should fall to zero within 5 minutes.
# (Threshold null → combiner is observe-only → no
# DISAGREE_DOWNGRADE → no path that emits validation errors
# from the combiner side.)
```

Wait 5 minutes. Re-check the failing SLI. If the alert is still firing AFTER 5 min, escalate to Compliance Veto Holder per §4.

---

## 4. Escalation paths

| Symptom | Owner | Channel |
|---|---|---|
| Alert fires; rollback proceeds cleanly within 5 min | SRE on-call | `#asoe-incidents` post-mortem ticket |
| Alert keeps firing after rollback | Compliance Veto Holder + Backend on-call | Page; correlate with `LLMCallTrace` rows for the affected window |
| `ComplianceVerdictMismatch` (any count) | Compliance Veto Holder | Page **before** rollback to capture the live state |
| Provider-side outage (`ShadowLLMUnavailability` >5%) | Tools Admin | Page; coordinate with Azure OpenAI status page |

---

## 5. Post-rollback checklist

Within 24 hours after rollback:

- [ ] File post-mortem in the SRE ticket queue with timeline + failing-metric values.
- [ ] Audit query: pull every record in the affected window where `llm_shadow_verdict.action == 'DISAGREE_DOWNGRADE'`. Cross-reference against reviewer disposition. Provide the count to Compliance.
- [ ] If the trigger was `ComplianceVerdictMismatch`, freeze X.2 ratification until Compliance reviews the mismatched cases and signs off on a re-flip.
- [ ] Update the bundle's `metadata.yaml::rollout` block with a note: `rolled_back_at: <iso>` so future readers see the history.
- [ ] Re-run the X.1 soak before the next X.2 attempt (Domain SME ask per the workshop pre-read).

---

## 6. Re-flip procedure

When Compliance and SRE both sign off on a re-flip:

1. Reverse the rollback: `kubectl edit configmap asoe-shadow-llm-bundle` → set `financial_impact_threshold_usd: 10000` (or `500`).
2. SIGHUP per §3.1.A.
3. Watch the same SLIs from §2 for the first 24 hours. Page on first sustained breach.

Re-flipping requires the same approval chain as the original X.2 ratification (Compliance + SRE + on-call awareness). It is NOT an SRE-unilateral decision.

---

*This runbook is authored 2026-05-09 alongside the ADR-039 X.2 code-path landing. Review cadence: every Compliance workshop or after any rollback event, whichever comes first.*
