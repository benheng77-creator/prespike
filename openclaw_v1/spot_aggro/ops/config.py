"""Config loader — thin re-export of shared.config.

Phase 11n-9-q — apex_omega.config was collapsed into shared.config.
"""

from __future__ import annotations

from shared.config import (  # noqa: F401
    coin_meta,
    load,
    statarb_pairs,
    universe_symbols,
)
