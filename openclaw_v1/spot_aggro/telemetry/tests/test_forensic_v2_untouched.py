"""
Hard guard: the telemetry package must never import from forensic_v2.
The frozen forensic_v2 package is read-only. Its output payloads are
sinked by *callers* (spot router endpoints), never from this module.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from spot_aggro.telemetry import (
    config as tcfg,
    emitter as te,
    metrics as tm,
    questdb_sink as tq,
    sentry_bridge as ts,
)


_MODULES = (tcfg, te, tm, tq, ts)


_FORBIDDEN_PATTERNS = (
    "import forensic_v2",
    "from forensic_v2",
    "from spot_aggro.forensic_v2",
    "import spot_aggro.forensic_v2",
    "spot_aggro.forensic_v2.",
)


def test_no_forensic_v2_import_in_telemetry() -> None:
    for mod in _MODULES:
        src = Path(inspect.getsourcefile(mod)).read_text(encoding="utf-8")
        for pat in _FORBIDDEN_PATTERNS:
            assert pat not in src, (
                f"{mod.__name__} references forensic_v2 via {pat!r} — "
                f"telemetry never imports the frozen package"
            )
