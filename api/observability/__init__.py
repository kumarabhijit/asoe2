"""PARITY-5 — observability seams.

This package consolidates the cross-cutting observability pieces:

  * `audit_bearing` — decorator + lint that locks state-mutating
    routes as SOX-relevant.
  * `otel` — OpenTelemetry FastAPI instrumentation, OTLP exporter
    pointed at APPLICATIONINSIGHTS_CONNECTION_STRING. Init only when
    the env var is set (no-op locally).
  * `alerts` — pre-defined alert catalogue (zero-highlight,
    layer2-open-rate, etc.) so dashboards can wire to stable names
    before thresholds are tuned.
  * `log_redaction` — PII scrubber the structured logger applies
    before any record reaches the Application Insights exporter.

The legacy `observability/` package at the repo root (langfuse_sink,
tracer) is preserved untouched — that's the LLM/recipe trace surface,
this is the platform/request surface.
"""
