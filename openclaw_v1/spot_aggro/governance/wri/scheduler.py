"""
WRI cadence scheduler.

Stateless helpers that, given a `now_ms` and the last-run timestamps for
each cadence (read from the store), return which cadences are due.

The engine loop (Phase 8) calls `due_cadences(now_ms)` every heartbeat and
triggers a run for each returned name. No threading here — the scheduler
is data-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError as _exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required for WRI scheduler") from _exc

from .. import store


DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "governance.yml"
)


@dataclass(frozen=True)
class CadenceSpec:
    name: str
    interval_s: int
    window_s: int


@dataclass(frozen=True)
class WRISchedule:
    enabled: bool
    cadences: tuple[CadenceSpec, ...]
    cluster_min_trades: int
    drag_floor_pct: float
    tier_min_trades_for_likely: int
    tier_min_trades_for_proven: int

    @staticmethod
    def load(path: Optional[Path] = None) -> "WRISchedule":
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        if not p.exists():
            raise FileNotFoundError(f"governance config not found: {p}")
        with p.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if raw.get("engine") != "spot_aggro":
            raise ValueError("governance config must declare engine=spot_aggro")
        wri = raw.get("wri") or {}
        cad_raw = wri.get("cadences") or {}
        cadences: list[CadenceSpec] = []
        for name in ("micro", "operational", "full"):
            c = cad_raw.get(name) or {}
            cadences.append(CadenceSpec(
                name=name,
                interval_s=int(c["interval_s"]),
                window_s=int(c["window_s"]),
            ))
        return WRISchedule(
            enabled=bool(wri.get("enabled", True)),
            cadences=tuple(cadences),
            cluster_min_trades=int(wri.get("cluster_min_trades", 5)),
            drag_floor_pct=float(wri.get("drag_floor_pct", 0.1)),
            tier_min_trades_for_likely=int(wri.get("tier_min_trades_for_likely", 10)),
            tier_min_trades_for_proven=int(wri.get("tier_min_trades_for_proven", 30)),
        )

    def cadence(self, name: str) -> CadenceSpec:
        for c in self.cadences:
            if c.name == name:
                return c
        raise KeyError(name)


def due_cadences(
    now_ms: int,
    schedule: WRISchedule,
) -> list[CadenceSpec]:
    """Return cadence specs whose last-run timestamp is older than their
    interval. A cadence that has never run is always due.

    Reads last-run ts from `spot_aggro_wri_runs`; no writes here.
    """
    if not schedule.enabled:
        return []
    due: list[CadenceSpec] = []
    for c in schedule.cadences:
        last = store.last_wri_run(c.name)
        if last is None:
            due.append(c)
            continue
        if now_ms - last.ts_ms >= c.interval_s * 1000:
            due.append(c)
    return due
