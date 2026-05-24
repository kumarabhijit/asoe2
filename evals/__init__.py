"""Eval harness for LLM-consumed steps (classification, extraction, draft,
compliance-shadow).

The metrics core (``evals.metrics``) is pure-stdlib and deterministic so it can
be unit-tested without a live model or project LLM dependencies. Dataset
loading and the model-coupled scorers live in ``evals.harness`` (added behind
the eval-first gate).
"""
