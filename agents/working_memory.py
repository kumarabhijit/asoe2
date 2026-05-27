"""ADR-038 §5.3 — working-memory builder for the L3 Case Agent.

Constructs the per-turn context the agent's LLM call sees. Order
is binding for prompt-cache stability:

  1. L4 system instructions (small, generic; cached)
  2. Active L0 SKILL.md (cached per skill)
  3. Anchor examples (loaded into cached prefix)
  4. On-demand example manifest (one-line summaries; cached)
  5. Per-turn case working memory (compacted summary + last N actions)
  6. Current event payload (per-turn)
  7. Tool surface descriptors (stable; cached)

Items 1–4 + 7 are the *cacheable prefix*. Items 5–6 vary per turn.
The L4 harness assembles in this exact order; CI fitness tests
fail on regression (per ADR-038 §5.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from agents.case_tools import ToolRegistry
from contracts.models import OrderCase, OrderEvent


SYSTEM_PROMPT = """\
You are the ASOE Case Agent. Your job is to coordinate deterministic
primitives (check_*, validate_*) and, when needed, request human
review (escalate, request_clarification_email) to drive a single
order's case to a terminal state.

Rules:
  * You may ONLY call tools registered in the surface below. No
    free-form action.
  * When you have enough information, call declare_done(status=...)
    OR escalate(reason_code=..., target_role=...) OR
    request_clarification_email(template=..., fields=...).
    Each halts the loop.
  * Conservative bias: when you cannot determine the right action,
    escalate. Do not invent business logic.
  * Compliance Shadow runs around action-emitting tools. The harness
    enforces the gate; you cannot bypass it.
"""


@dataclass
class WorkingMemoryFrame:
    """Per-turn context assembled by ``build_working_memory()``.

    Each field is a separate prompt segment; the harness composes
    them in §5.3 order. The shape is what's tested; production
    serialises this into the LLM-provider's prompt format.
    """

    system_prompt: str
    skill_md: str  # active skill bundle's SKILL.md verbatim
    anchor_examples: List[str]  # body text of 0–2 anchor examples
    example_manifest: List[Dict[str, str]]  # name + summary lines only
    case_summary: Dict[str, Any]
    last_actions: List[Dict[str, Any]]
    current_event: Dict[str, Any]
    tool_descriptors: List[Dict[str, str]]


def _load_active_skill_bundle(skill_name: str) -> Optional[Path]:
    """Locate the L0 bundle directory for a skill name."""
    bundle = Path("knowledge/skills") / skill_name
    if bundle.is_dir() and (bundle / "SKILL.md").exists():
        return bundle
    return None


def _read_skill_md(bundle: Path) -> str:
    return (bundle / "SKILL.md").read_text(encoding="utf-8")


def _load_metadata(bundle: Path) -> Dict[str, Any]:
    manifest = bundle / "metadata.yaml"
    if not manifest.exists():
        return {}
    with manifest.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    return loaded if isinstance(loaded, dict) else {}


def _resolve_anchor_examples(bundle: Path, metadata: Dict[str, Any]) -> List[str]:
    """Return the bodies of the bundle's anchor examples (≤ 2 per
    ADR-038 §5.4 rule 1). Each is a verbatim file read; the loader
    enforces the runtime-allowlist for path resolution."""
    anchors = metadata.get("anchor_examples") or []
    if not isinstance(anchors, list):
        return []
    out: List[str] = []
    for entry in anchors[:2]:  # defensive cap
        if not isinstance(entry, dict):
            continue
        rel = entry.get("file")
        if not isinstance(rel, str):
            continue
        path = bundle / rel
        if path.exists() and path.is_file():
            out.append(path.read_text(encoding="utf-8"))
    return out


def _on_demand_example_manifest(metadata: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return the one-line summaries the agent sees in the cached
    prefix. Bodies are loaded on demand via the load_example tool
    (deferred to a later commit; the manifest still ships)."""
    on_demand = metadata.get("on_demand_examples") or []
    if not isinstance(on_demand, list):
        return []
    out: List[Dict[str, str]] = []
    for entry in on_demand:
        if not isinstance(entry, dict):
            continue
        out.append({
            "name": str(entry.get("file") or ""),
            "summary": str(entry.get("summary") or ""),
        })
    return out


def _case_summary_dict(case: OrderCase) -> Dict[str, Any]:
    return {
        "case_id": case.case_id,
        "tenant_id": case.tenant_id,
        "origin": case.origin,
        "source_channel": case.source_channel,
        "customer_id": case.customer_id,
        "customer_po_number": case.customer_po_number,
        "sales_order_id": case.sales_order_id,
        "status": case.status,
        "tier": case.tier,
        "opened_at": case.opened_at,
        "sla_deadline": case.sla_deadline,
        "compacted_summary": case.working_memory_summary,
    }


def build_working_memory(
    *,
    case: OrderCase,
    skill_name: str,
    current_event: OrderEvent,
    last_actions: Optional[List[Dict[str, Any]]] = None,
    tool_registry: ToolRegistry,
) -> WorkingMemoryFrame:
    """Assemble the per-turn working memory.

    The skill_name is determined upstream by the harness's existing
    skill-selection logic (skills/loader.py::select_for_event); this
    builder loads the bundle's SKILL.md + anchor examples + manifest
    summaries in cache-friendly order.

    Returns a typed frame. The harness serialises it to the
    LLM provider's preferred format (Anthropic messages, OpenAI
    chat completions, etc.).
    """
    bundle = _load_active_skill_bundle(skill_name)
    if bundle is None:
        skill_md = ""
        anchor_bodies: List[str] = []
        manifest: List[Dict[str, str]] = []
    else:
        skill_md = _read_skill_md(bundle)
        metadata = _load_metadata(bundle)
        anchor_bodies = _resolve_anchor_examples(bundle, metadata)
        manifest = _on_demand_example_manifest(metadata)

    return WorkingMemoryFrame(
        system_prompt=SYSTEM_PROMPT,
        skill_md=skill_md,
        anchor_examples=anchor_bodies,
        example_manifest=manifest,
        case_summary=_case_summary_dict(case),
        last_actions=list(last_actions or []),
        current_event=current_event.model_dump(mode="json"),
        tool_descriptors=tool_registry.to_descriptors(),
    )


def cache_prefix_segments(frame: WorkingMemoryFrame) -> List[str]:
    """The cacheable prefix (items 1–4 + 7 from §5.3 order). Useful
    for tests that assert prompt-cache stability."""
    parts: List[str] = [
        frame.system_prompt,
        frame.skill_md,
    ]
    parts.extend(frame.anchor_examples)
    parts.extend(
        f"<<example {e['name']}>> {e['summary']}"
        for e in frame.example_manifest
    )
    parts.extend(
        f"<<tool {d['name']}>> {d['description']}"
        for d in frame.tool_descriptors
    )
    return parts


def per_turn_segments(frame: WorkingMemoryFrame) -> List[str]:
    """The per-turn payload (items 5–6 from §5.3 order)."""
    import json
    return [
        f"<<case_summary>> {json.dumps(frame.case_summary, sort_keys=True)}",
        f"<<last_actions>> {json.dumps(frame.last_actions, sort_keys=True)}",
        f"<<current_event>> {json.dumps(frame.current_event, sort_keys=True)}",
    ]
