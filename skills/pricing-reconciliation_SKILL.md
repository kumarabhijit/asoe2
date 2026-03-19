---
name: pricing-reconciliation
description: Analyzes and resolves CPG pricing discrepancies using deterministic ERP recipes and retailer contract RAG.
metadata:
  version: 1.2.2
  author: CPG Expert-Systems-Architect
  required_tools: [mcp-sap-connector, mcp-pinecone-search]
  recipes: [PriceAdjustmentRecipe.py, CreditHoldReleaseRecipe.py]
  constrained_generation: [Guidance, Outlines]
---
# Skill: CPG Pricing Exception Management
## 1. Overview
This skill is triggered when an inbound order (EDI 850) contains a price variance compared to the SAP Master Data.
It determines the intent of the discrepancy and applies a deterministic Recipe to execute the fix.

## 2. Reasoning Loop
1. Identify the Gap.
2. Query context for active promotions and retailer-specific contract terms.
3. Classify intent.
4. Select the exact recipe required. Do not improvise the logic.

## 3. Constrained Generation Policy
Use Guidance / Outlines for all machine-consumed outputs:
- intent labels must be constrained to the allowed intent enum
- recipe names must be constrained to registered recipes only
- shadow verdicts must be constrained to GREEN / YELLOW / RED
- shadow outputs should include reasons and policy hits

## 4. Recipe-to-Intent Mapping
- Contractual Correction -> `PriceAdjustmentRecipe.py`
- Credit Block -> `CreditHoldReleaseRecipe.py`
- Mass Pricing Error -> `FAIL_TO_HUMAN`

## 5. Execution Protocol
Before calling any recipe, check the Compliance Shadow.
If the shadow returns RED, halt execution and explain the compliance breach.

## 6. Output Requirements
Always include the Recipe Execution Log snippet for audit traceability.
