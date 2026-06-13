# ADR-046: `azure_replica` SAP gateway driver (deferred)

**Status:** Proposed / Deferred (not scheduled)
**Date:** 2026-06-13
**Deciders:** Principal AI/Agentic Engineering Architect; Compliance Engineer; SRE/Azure; Domain Modeller.
**Applies to (when accepted):**
* asoe2: a new `gateways/sap_azure_replica.py` adapter behind the existing
  SAP port, `ASOE_SAP_DRIVER=azure_replica` routing in
  `api/preprod_gateways.py`, `infra/main.bicep` (the replica Postgres +
  CDC landing schema), `compliance/audit_bearing_registry.yaml` (parity
  sign-off — CODEOWNERS-gated).

**Related:**
* RFC: `asoe-ui/docs/synthetic-data-placement-rfc.md` — Decision A (now,
  done) vs **Decision B (this ADR, deferred)**.
* Handoff: `asoe-ui/docs/asoe2-execution-brief.md` §5 (Decision B).
* Draft replica target (frozen reference, do **not** build on):
  `asoe-ui/data/synthetic/*.sql` (raw `vbak`/`vbap`/`konv`… DDL).

---

## Context

asoe2 reads SAP **live via OData** today (`gateways/sap_live.py`,
`ASOE_SAP_DRIVER=s4hana`), returning domain `GatewayResponse` objects
(`sap_order` / `sap_doc` / `sap_contract`). There is **no SAP-replica
Postgres** in `infra/main.bicep` — the only Flexible Server there is the
ASOE *application* DB (cases / audit hash-chain / pgvector). The PgBouncer
note in the bicep ("MUST land before the Phase 6.3 SAP sub-phase — real
SAP fan-out") confirms SAP arrives via live connectors, not replication.

A recurring alternative is the **"replicate SAP → Azure Postgres and read
the DB"** pattern: an SAP SLT / Azure Data Factory CDC pipeline lands the
SD tables (`vbak`, `vbap`, `konv`, `kna1`, `knvv`, …) into an Azure
Database for PostgreSQL schema (`sap_replica`), and ASOE reads that DB
instead of OData. The superseded asoe-ui `data/synthetic/` work modelled
exactly this landing schema. It is a legitimate architecture **change**,
not a fixture-placement detail — so it is captured here as a standalone,
deferred ADR rather than built opportunistically.

This ADR is the durable record of that option. **Nothing here is wired
into anything** until the ADR is accepted.

## Decision (when/if accepted)

Add a sibling adapter to `sap_live.py` selected by
`ASOE_SAP_DRIVER=azure_replica`:

1. **Adapter.** `gateways/sap_azure_replica.py` reads the `sap_replica`
   Postgres schema and maps rows → the **same** `GatewayResponse` /
   `contracts.models` shapes the OData driver returns. The SAP **port
   contract is unchanged**; only the adapter differs. Recipes,
   orchestration, and the composer require **zero** changes — identical
   to how `gateways/msgraph_intake.py` slotted behind the `email_intake`
   port.
2. **Schema is reconciled, not hand-authored.** The replica DDL **must**
   match what the real SLT/ADF pipeline lands, captured in `infra/`
   alongside the bicep. The asoe-ui draft DDL is a *starting point only*
   (see "Draft replica target" below).
3. **Parity tests.** `azure_replica` and `s4hana` must produce
   **identical** `GatewayResponse`s for the same scenario (golden parity
   suite), the same way recorded-vs-live shadow diffs are gated for the
   email connector.
4. **Compliance sign-off.** Any audit-bearing field served from the
   replica is registered in `compliance/audit_bearing_registry.yaml`
   (CODEOWNERS-gated) before the driver serves real-tenant traffic.
5. **Infra.** `infra/main.bicep` gains the replica Postgres + private
   networking (the VNet/private-endpoint upgrade path already sketched in
   the bicep PARITY-0 comment), behind a `deploySapReplica` gate so it
   ships independently.

The local sandbox stays on the **domain-shaped** seed
(`tests/sandbox/seed.py` ← `fixtures/scenarios/catalog.yaml`, Decision A).
`azure_replica` is a pre-prod/prod SAP read path, not a sandbox concern.

## Draft replica target (attached, frozen)

The asoe-ui `data/synthetic/` dataset is the **first draft** of the
`sap_replica` landing schema and is attached as the starting reference:

| File | Contents |
| --- | --- |
| `00_schema.sql` | `sap_replica` DDL — `t001w`, `kna1`, `knvv`, `vbak`, `vbap`, `konv`, `mara`, `marc`, stock, intake-staging, with the `_source_system` / `_replicated_at` / `_replication_op` CDC envelope columns |
| `10_master_data.sql` | plants, customers, credit, materials, UoM, MOQ/max-qty, stock, pricing conditions |
| `20_sales_documents.sql` | sales orders, items, schedule lines, partners, document pricing, deliveries |
| `30_intake_channels.sql` | inbound EDI 850 staging + customer-inbox email staging |
| `40_asoe_lineage.sql` | `zasoe_exception_link` — maps each UI exception to its SAP documents |

**Canary to remove before acceptance:** the draft widens `konv.kschl`
from SAP's native `varchar(4)` to `varchar(5)` to carry a demo `ZPROM`
condition type. That is a *fit-the-demo* hack — the real column width
must come from the production SLT/ADF DDL, not from what the fixtures
happened to need. Treat any such shape divergence as a parity defect.

## Consequences

* **Pro.** Decouples ASOE read latency/availability from live SAP OData;
  enables heavy analytical reads against a replica without loading S/4HANA.
* **Pro.** Port contract unchanged → the switch is an adapter swap + an
  infra gate, reviewable in isolation.
* **Con.** Adds a CDC pipeline (SLT/ADF) and a second SAP read path to
  operate, monitor, and keep at parity — replication lag becomes an
  audit-relevant correctness concern (the OData path is read-through).
* **Con.** The replica schema is a new compliance surface; every
  audit-bearing field served from it needs registry sign-off.

## Status / next step

Deferred. Adopt only if/when replication replaces OData as the SAP read
path. Until then, Decision A (the domain-shaped catalog + seed) is the
shipped answer and this ADR is the parked design.
