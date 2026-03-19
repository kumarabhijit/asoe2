from .router import get_constrained_backend
from .fallback_backend import DeterministicFallbackBackend
from .guidance_backend import GuidanceRegexBackend

# OutlinesConstrainedBackend is intentionally NOT imported here at module level.
# It depends on `outlines` and `transformers` which are heavy optional
# dependencies.  Import it directly when USE_OUTLINES_BACKEND=1 is required.
