"""ADR-038 — L3/L4 agent + harness layer.

Phase H.4 ships the attachment-extractor L2 primitive
(agents/primitives/extract_attachment.py). Phase H.5 ships the
Case Agent loop (agents/case_agent.py), the tool surface
(agents/case_tools.py), and the working-memory loader.

Until Phase H.5 lands, this package contains only the L2 primitives
the agent will eventually consume.
"""
