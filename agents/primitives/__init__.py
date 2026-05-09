"""L2 bounded LLM primitives — narrow, constrained-output, cacheable.

ADR-038 §4 / Layer L2: each primitive is one LLM call (or a deterministic
pipeline that wraps one) with a structured output schema. Primitives are
invoked by the L3 Case Agent as tools; they never recurse.
"""
