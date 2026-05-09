-- ADR-038 Phase H.5 — case_events replay table.
--
-- Persists every (tool_call, tool_result) pair the L4 harness
-- observes during a Case Agent step. The agent does not write
-- here directly; the harness does, so audit replay reads from
-- a single canonical source.
--
-- Schema mirrors `agents.harness.ToolCallReplayEntry` field-for-
-- field. Append-only — there is no UPDATE / DELETE path on this
-- table by application convention. Compaction operates on a
-- DERIVED summary stored on `order_case.working_memory_summary`;
-- the underlying events are retained verbatim per ADR-038 §7.4.
--
-- Forward-only DDL.

CREATE TABLE IF NOT EXISTS case_events (
    event_id          TEXT PRIMARY KEY,
        -- harness-supplied: `{case_id}:{iteration}:{call_index}`
    case_id           TEXT NOT NULL REFERENCES order_case(case_id),
    tenant_id         TEXT NOT NULL,
    occurred_at       TEXT NOT NULL,
        -- ISO-8601 UTC; harness wall-clock time, not the LLM's
        -- timestamp. Replay-determinism requires the harness
        -- observation order, not the inner-loop emission order.
    tool_name         TEXT NOT NULL,
    tool_call         JSONB NOT NULL,
        -- Full ToolCall payload (tool_name + arguments). Pydantic
        -- model dumps to a small dict; storing as JSONB keeps
        -- query-by-arg cheap.
    tool_result       JSONB NOT NULL,
        -- Full ToolResult payload (status + data + error +
        -- cost_usd). Cost is queryable for SLI dashboards.
    outcome           TEXT NOT NULL
        -- The AgentRunOutcome at the END of the agent step the
        -- call belonged to. Repeats per call within one step.
);

-- Audit-replay query: one case in chronological order.
CREATE INDEX IF NOT EXISTS idx_case_events_case_time
    ON case_events (case_id, occurred_at);

-- Tenant-isolation query: SLI dashboards filter by tenant.
CREATE INDEX IF NOT EXISTS idx_case_events_tenant_time
    ON case_events (tenant_id, occurred_at);

-- Per-tool counters (e.g. extract_attachment cost analysis).
CREATE INDEX IF NOT EXISTS idx_case_events_tool
    ON case_events (tenant_id, tool_name);
