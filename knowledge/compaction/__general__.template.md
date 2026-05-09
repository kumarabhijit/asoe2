# Compaction template — fallback (general)

ADR-038 Phase H.7. Deterministic per-event-type summarisation. This
template is the fallback when no event-type-specific template
matches; production CODEOWNERS-gated edits land per-event-type
templates as patterns surface in real traffic.

## Format

For each compactable event the L4 harness emits ONE line:

    [<event_type>@<timestamp>] <key=value, key=value, ...>

The compactor assembles N lines into a single block bounded at
~2000 tokens (ADR-038 §7.4). Original events are retained verbatim
in the database; this is the working-memory replacement only.

## Per-line content

Each event must surface, at minimum:
  * event_type (the type literal — drives the agent's recognition)
  * outcome (status / classification / action; ADR-038 §6.4 vocab)
  * 1–3 audit-bearing key fields specific to the event type

Anything not in the bounded set is dropped from the compaction;
auditors retain the verbatim event in episodic memory.
