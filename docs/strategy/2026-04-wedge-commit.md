# ASOE — Wedge Commit (April 2026)

**Status:** Committed
**Date:** 2026-04-19
**Decision owner:** Founder
**Supersedes:** all prior informal strategy notes pre-dating this file
**Review cadence:** Re-examine at 30, 60, 90 days against the milestones below. Hard gate at day 90 on design-partner LOI.

---

## 1. TL;DR

ASOE commits to the **Upstream Wedge** — an agent that detects and scores order-exception risk inside SAP/Oracle before it becomes a cost event — delivered **read-only** in v0.1, across a **dual-track geography** (India via CavinKare as first design partner; US via Walmart/Kroger EDI for a named US design partner to be closed in the same 90-day window).

We are **not** building: deduction resolution (Glimpse's lane), GL write-backs, SOX/audit workflows, BPO channel deals, or a general "governance platform" — all of those are downstream of first paid reference.

---

## 2. Premises we tested and locked

| # | Premise | Status | Note |
|---|---|---|---|
| P1 | The category is contested, not greenfield | **Locked** | Glimpse, Confido, SAP Joule, Oracle Fusion Agents, and at least three Y Combinator entrants are actively selling into adjacent surfaces. Greenfield framing is retired. |
| P2 | Moat is either a vertical wedge or the governance/audit layer; horizontal "AI for ops" is not defensible | **Locked** | Horizontal positioning is abandoned. |
| P3 | One surgically-chosen design partner beats three | **Partially locked** | Founder elected to pursue 2–3 design partners. Risk: dilutes focus. Mitigation: require that all design partners share the same wedge shape (exception risk detection on SAP), not merely the same industry. |
| P4 | ASOE sits on top of SAP/Oracle, not against them | **Locked** | Read-only v0.1 posture codifies this. |

---

## 3. Landscape (as of April 2026)

**Direct-adjacent competitors (active):**
- **Glimpse** — deduction resolution for US CPG. Strongest in post-shipment financial recovery.
- **Confido** — trade-promotion and deduction management, overlapping Glimpse.
- **SAP Joule (agents)** — native, bundled, default-choice risk. Weakest on exception-specific workflows, strongest on distribution.
- **Oracle Fusion Agents** — equivalent risk inside Oracle estates.
- **Multiple YC/pre-seed entrants** — each picking a narrow exception surface.

**Adjacent but non-competing:**
- Celonis (process mining, not real-time exception resolution)
- UiPath / Automation Anywhere (RPA, not agentic reasoning)
- BPOs (Genpact, WNS, Cognizant, Infosys BPM) — channel candidates, not competitors

**White-space we are claiming:**
- Pre-event exception detection (upstream of the chargeback / deduction / scheme-dispute)
- Multi-trade-partner (retailer + distributor + dealer) scope in a single agent
- Read-only posture that sidesteps the SOX/audit conversation in v0.1

---

## 4. The three approaches we considered

| Approach | Description | Verdict |
|---|---|---|
| A — Upstream Wedge | Read-only exception detection + risk scoring + daily digest. Read-only on SAP/Oracle. | **Chosen.** |
| B — Governance-First Platform | SOX/audit layer for any agentic action. | Deferred. Emerges as year-2 platform narrative once A has reference customer. |
| C — Embedded BPO Killer | ASOE inside Genpact / WNS / Cognizant as their AI layer. | Deferred to month 4+. BPO conversations only resume after a standalone product exists. |

Rationale for A: fastest path to answering "who is the buyer" (90 days, not 12 months); asymmetric advantage (less crowded than deductions, no head-on collision with SAP Joule); read-only sidesteps 80% of the audit conversation; natural upsell to deductions and governance later.

---

## 5. Committed scope for v0.1 (first 90 days)

### What we WILL build

**Connectors (read-only):**
- SAP S/4HANA + ECC read connector: customer master, open sales orders, inventory snapshot, open POs, ASN status, scheme master, pricing conditions
- Oracle EBS / Fusion read connector (same object surface, parked to day 45 if SAP connector slips)
- EDI parsers: 850 / 855 / 856 / 810 for Walmart (phase 1), Kroger (phase 2). Indian retail analogue: Reliance Retail, DMart, Blinkit PO/ASN formats to be scoped against CavinKare's actual data in week 2.

**Exception detection engine (read-only scoring):**
- Fulfillment risk per open PO (ASN-vs-inventory-vs-lead-time)
- Scheme dispute risk per dealer claim (master-mismatch detection)
- Cold-chain vs. ambient back-order prediction
- Pricing mismatch detection at modern-trade gates
- Dealer back-order aging

**Outputs:**
- Daily exception digest — Slack + Teams + email
- Per-exception drill-down in asoe-ui
- Exception-level audit trail (reason chain, confidence, data lineage)

### What we will NOT build

- **No GL writes.** No posting. No approvals workflow.
- **No deduction resolution.** Read-only scoring of deduction risk is acceptable; actioning deductions is Glimpse's lane and we do not enter it in v0.1.
- **No SOX/audit dashboards.** Read-only posture means we do not need them yet.
- **No BPO partnership track.** Do not respond to Genpact/WNS/Cognizant inbound until month 4.
- **No horizontal "AI for ops" marketing.** Every surface-facing asset (deck, site, email) must lead with the CPG + SAP + exception wedge.

---

## 6. Geography: dual-track from day one

**Choice:** India (CavinKare) AND US (Walmart/Kroger EDI) from day one.

**Honest risk:** Pre-product startups running two parallel go-to-markets usually make neither customer happy. This is a known tax on the decision.

**Mitigations required:**
1. **Shared core, split parsers.** The exception-detection engine must be a single codebase. Only the parsers (EDI 850 for Walmart; Reliance Retail PO formats for CavinKare) diverge. If the core bifurcates, we have failed.
2. **Different people on each demand-gen motion.** Founder owns CavinKare conversation end-to-end. A second named owner (founder or first commercial hire) owns the US outreach. No single-person context-switching between buyers.
3. **Single weekly standup** comparing exception-shape overlap across the two accounts. If shapes diverge more than 30% at week 6, one track is cut.
4. **Shared risk scoring semantics.** The exception schema is the contract. If we cannot express a CavinKare scheme dispute and a Walmart OTIF risk in the same exception record, the design is wrong.

**Tripwire to collapse to single-track:** If by day 60 we have a CavinKare paid LOI and zero US design-partner conversations in active diligence, we cut US work and become India-first. Re-open US in phase 2 from a position of strength.

---

## 7. First design partner: CavinKare

**Target contact:** Akash (COO / Head of Ops)
**Company:** CavinKare Pvt Ltd — Indian CPG, personal care + dairy + food, ~60k dealer footprint, pushing toward 25% e-commerce revenue
**ERP:** SAP (to be confirmed on first call — version + module footprint)
**Pain shape:**
- Scheme disputes across 60k dealers
- Cold-chain vs. ambient back-orders
- Pricing mismatches at modern-trade gates
- Customer inquiries volume growing with e-com ramp

**Posture in v0.1:** Read-only. No writes into SAP. Agent watches, scores, and alerts. Write access is a v0.2 conversation earned after trust is established.

**Approved intro email (COO persona, read-only posture):**

> Hi Akash,
>
> As CavinKare pushes toward 25% e-com, your SAP-based order ops will absorb materially more exception volume — scheme disputes across your 60,000 dealers, cold-chain vs. ambient back-orders, pricing mismatches at modern-trade gates.
>
> We've built ASOE to detect and score these exceptions inside your SAP exception queue — read-only, no writes — before they reach your ops team. For an ops footprint your size, we believe there's meaningful capacity to unlock and scheme-dispute leakage to recover, without adding headcount.
>
> I'd love to walk through our approach and model the specific numbers with you in 20 minutes. Week of April 28 work?
>
> [Founder name]

Notes on email rationale:
- No "designed-in benchmark" asterisk. Unverified percentage claims with a footnote admitting they are synthetic destroy credibility with a COO.
- No fabricated rupee figure. The rupee conversation happens in the meeting with CavinKare's real data, not in the cold email.
- "Read-only, no writes" is a deliberate disarm. A COO who has been pitched AI-in-SAP before will have been burned by write promises.
- CTA is a specific week, not an open-ended "worth 20 minutes."

**Second US design partner:** TBD. Target list to be drafted week 1: 20 mid-tier CPG VP Supply Chain / Customer Ops contacts, $500M–$3B revenue, Walmart/Kroger exposure. Goal: 1 active diligence conversation by day 30, signed LOI by day 90.

---

## 8. 90-day milestones

| Day | Commercial milestone | Product milestone |
|---|---|---|
| 7 | Akash (CavinKare) meeting scheduled; US target list drafted (20 names) | SAP read connector scoped against CavinKare's actual module footprint |
| 30 | CavinKare discovery complete, scope document signed; 2 US prospects in active diligence | Exception detection engine v0 running on synthetic data |
| 60 | CavinKare pilot LOI signed OR tripwire triggered (collapse to India-first) | CavinKare data connector live in sandbox; first exception digest delivered |
| 90 | CavinKare paid pilot live; 1 US design-partner LOI signed (or pipeline decision) | First weekly exception digest to CavinKare ops team; US EDI parsers operational in staging |

**Hard gate at day 90:** If no paid pilot is live by day 90, convene a strategy reset. Do not drift.

---

## 9. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Dual-track splits focus, neither customer ships | High | High | See §6 mitigations. Day-60 tripwire. |
| CavinKare's SAP footprint is older ECC with custom Z-tables | Medium | Medium | Confirm module + version on discovery call week 1. Budget 2 extra weeks if heavy customization. |
| SAP Joule pre-empts at CavinKare via account manager | Medium | High | Lead every conversation with "we sit alongside Joule, not against it." Read-only posture helps. |
| Glimpse or Confido enters India | Low (12 months) | Medium | Not a 90-day risk. Revisit at quarterly review. |
| Founder becomes single-thread bottleneck on CavinKare | High | High | Founder blocks 10 hrs/week on CavinKare from day 1; one named backup owner for continuity. |
| Akash is not the actual economic buyer | Medium | Medium | First meeting ends with "who else should be in the next conversation?" — map CFO, CIO, Supply Chain VP in meeting 1. |
| Read-only posture blocks clear value demonstration | Medium | Medium | Exception digest must quantify rupee-at-risk per exception from day one. Reading is only valuable if the buyer can quote the rupee number back. |

---

## 10. What would cause us to abandon this commit

- No paid pilot by day 120 (30 days past the hard gate)
- CavinKare pilot live but the COO cannot quote a single rupee number ASOE surfaced in the first 30 days of operation
- A US design partner insists on write access as a precondition AND we cannot close an India paid pilot either

In any of these cases: retreat to Approach B (governance platform) or C (BPO embed) and re-plan from first principles.

---

## 11. Open questions parked for the 30-day review

1. Does CavinKare run SAP S/4HANA or ECC? Module footprint?
2. Is Akash the economic buyer or the operational sponsor? If the latter, who is above him?
3. Which US retailer EDI spec do we build FIRST against — Walmart, Kroger, Target, Costco? Decision requires a named US design partner.
4. Price point for v0.1 paid pilot — flat fee, per-exception, per-ERP-connector? First offer shape to be drafted by day 20.
5. Data residency for CavinKare (India data sovereignty requirements on SAP exception data)?

---

*This document is the strategy source-of-truth as of the date above. Engineering spec follows once design-partner LOI is signed.*
