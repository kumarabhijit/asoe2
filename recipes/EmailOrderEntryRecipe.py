from __future__ import annotations

# Email Order Entry Recipe — DEPRECATED RE-EXPORT (ADR-034 §6.3).
#
# This module is preserved as a back-compat shim during the
# transitional window defined in ADR-034 §6.2 (deadline:
# 2026-08-12). New code imports from
# `recipes.ManualOrderIntakeRecipe`. The wildcard re-export below
# keeps every existing import resolving without surprise; the
# module itself contains no business logic.
#
# Removal: on or after the §6.2 deadline, in the same PR that
# flips the hard rejection on the legacy event_type, this file is
# deleted and `recipes/registry.py` retires the
# `"EmailOrderEntryRecipe.py"` registry key.
from recipes.ManualOrderIntakeRecipe import *  # noqa: F401,F403
from recipes.ManualOrderIntakeRecipe import (
    classify_manual_order_intake as classify_manual_order_intake,
)

# Legacy function-name alias so callers that did
# `from recipes.EmailOrderEntryRecipe import classify_email_order_entry`
# keep resolving. Same function object — not a wrapper.
classify_email_order_entry = classify_manual_order_intake
