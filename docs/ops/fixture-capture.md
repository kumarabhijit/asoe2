# Connector fixture-capture process (PARITY-6)

> Who captures gateway fixtures, on what data, on what cadence.

When a real-connector sub-phase (Graph / SAP / OMS / Document
Intelligence) is wired in, we capture sanitised request/response
fixtures so the CI matrix and PR-time regression tests can run
against deterministic inputs without round-tripping the live
upstream.

The capture script is `scripts/record_gateway.py`. This doc covers
the operational contract — who runs it, on what data, how the
results are reviewed, and how they're committed.

## Roles

| Role | Responsibility |
|---|---|
| **Platform engineer** | Runs the capture script, holds the credentials, sanitises the recorded payloads, opens the PR. |
| **Compliance reviewer** | Reviews the sanitisation diff in the PR; checks no PII slipped through (CODEOWNERS gate). |
| **Integration owner** | Reviews the shape diff vs the previous capture; runs the nightly fixture-vs-live diff alert. |
| **SAP Basis / Graph admin** | Provides the read-only credentials; rotates them on the documented cadence. |

## Cadence

* **Weekly** — Platform engineer reruns the capture against the
  current preprod sources to detect schema drift early. The nightly
  diff alert (Integration owner) flags drift between weekly captures
  in the meantime.
* **On schema change** — any upstream API contract bump triggers an
  immediate capture; the PR's title is `connector(<source>): fixture
  refresh for <change>`.
* **Before flipping a connector to real-only** — fresh capture; the
  Q9 shadow-mode threshold check uses these as the comparison
  baseline.

## Approved data sources

| Connector | Source | Notes |
|---|---|---|
| Graph (`msgraph_intake`) | Microsoft Graph sandbox tenant `asoe-fixtures.onmicrosoft.com` | Pre-loaded with synthetic vendor / customer mailboxes; no real customer addresses. |
| SAP (`sap_*`) | S/4HANA Cloud trial tenant `s4hanatrial-asoe` | Sandbox data only. Production SAP captures are forbidden — too much real customer PII. |
| OMS (`oms`) | Internal `oms-preprod` cluster | Per-tenant fixtures sourced from synthetic orders only. |
| Document Intelligence (`document_extraction`) | Hand-curated PO fixtures in `tests/eval/datasets/extraction_spatial/` | Documents are SYNTHETIC — vendor names, amounts, PO numbers are made-up. |

## Capture procedure

```bash
# 1. Activate the read-only credentials for the source.
export GRAPH_TOKEN=$(az account get-access-token --resource https://graph.microsoft.com --query accessToken -o tsv)
# (SAP and OMS use their own auth flows; see the per-connector READMEs.)

# 2. Run the recorder against the approved source.
python scripts/record_gateway.py \
  --gateway graph \
  --operation list_messages \
  --tenant fixtures \
  --output gateways/fixtures/graph/list_messages.json

# 3. Run the sanitiser.
python scripts/sanitise_fixture.py gateways/fixtures/graph/list_messages.json

# 4. Verify the resulting JSON contains no PII patterns.
python -c "
from api.observability.log_redaction import redact_pii
import json, sys
raw = open('gateways/fixtures/graph/list_messages.json').read()
if redact_pii(raw) != raw:
    print('PII pattern detected — re-sanitise', file=sys.stderr)
    sys.exit(1)
print('OK')
"
```

## Sanitisation checklist

Before committing any fixture, verify:

- [ ] No real customer / vendor names. Replace with `Acme Beverages`
      / `Walmart Stores Inc` from the synthetic-data palette.
- [ ] No real email addresses. Replace with `*@stub-*.example`.
- [ ] No real PO numbers. Use the `PO-<NNNN>` range used by the
      sandbox fixtures.
- [ ] No real dollar amounts that match a known production deal.
- [ ] Diff against the previous fixture committed for the same
      operation; flag any new field shape to the Integration owner.

## Per-connector CHANGELOG

Every fixture refresh appends a row to
`gateways/fixtures/<connector>/CHANGELOG.md`:

```
2026-MM-DD | <gateway>.<operation> | <captured-by> | <reason>
```

This is the schema-drift attribution trail. The nightly fixture-vs-
live diff alert pulls from this log when surfacing a drift event.

## When NOT to capture

* **During an incident** — production / preprod is in an unusual
  state; capturing it locks anomaly into the fixture.
* **From a customer's tenant** — strictly forbidden; even read-only
  access to a real-customer namespace creates a data-handling
  obligation we can't always discharge in CI.
* **Without coordinating with the integration owner** — they hold
  the nightly diff baseline; a silent capture skews the alert.
