from __future__ import annotations

import re
from dataclasses import dataclass
from typing import get_args

from constraints.specs import (
    AllowedIntent,
    AllowedRecipeName,
    AllowedResolutionAction,
    AllowedShadowStatus,
)


def _literal_to_regex(literal_type) -> str:
    """Generate a regex alternation from a Literal type's allowed values."""
    return "|".join(re.escape(v) for v in get_args(literal_type))


@dataclass
class GuidanceRegexBackend:
    def intent_regex(self) -> str:
        return _literal_to_regex(AllowedIntent)

    def shadow_verdict_regex(self) -> str:
        return _literal_to_regex(AllowedShadowStatus)

    def recipe_name_regex(self) -> str:
        return _literal_to_regex(AllowedRecipeName)

    def resolution_action_regex(self) -> str:
        return _literal_to_regex(AllowedResolutionAction)
