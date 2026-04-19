"""
Scenario composer — turn a user selection into a deterministic bar series.

Modes:
    single      one scenario, full window
    multi       N scenarios concatenated equally
    weighted    each scenario gets a fraction of the total bars by weight
    chained     explicit ordered list, optional repeat for full window
    randomized  permutation of selected scenarios with preserved weights

The composer is pure: same inputs + seed → same output bars. Price is
continuous across scenario joins (next regime starts at previous close).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable

from .scenarios import (
    SCENARIOS, ScenarioSpec, Bar, synth_ohlcv,
    SEC_PER_BAR_DEFAULT,
)


@dataclass
class ScenarioPick:
    code: str
    weight: float = 1.0


@dataclass
class ComposeSpec:
    mode: str                              # single | multi | weighted | chained | randomized
    picks: list[ScenarioPick]
    total_bars: int
    repeat_to_fill: bool = True            # for chained mode
    seed: int = 0
    sec_per_bar: int = SEC_PER_BAR_DEFAULT
    start_price: float = 50_000.0
    start_ts: int = 1_700_000_000


@dataclass
class ComposedSeries:
    bars: list[Bar]
    timeline: list[dict]   # [{code, start_idx, end_idx}, ...]
    seed: int


def _normalize(weights: list[float]) -> list[float]:
    s = sum(weights)
    if s <= 0:
        return [1.0 / len(weights)] * len(weights)
    return [w / s for w in weights]


def _split_bars(total: int, fractions: list[float]) -> list[int]:
    raw = [max(1, int(round(f * total))) for f in fractions]
    diff = total - sum(raw)
    # Distribute leftover (positive or negative) on the last segment
    if raw:
        raw[-1] = max(1, raw[-1] + diff)
    return raw


def compose(spec: ComposeSpec) -> ComposedSeries:
    if not spec.picks:
        raise ValueError("at least one scenario pick required")
    for p in spec.picks:
        if p.code not in SCENARIOS:
            raise ValueError(f"unknown scenario code: {p.code}")
    if spec.total_bars <= 0:
        raise ValueError("total_bars must be > 0")

    mode = spec.mode.lower()
    rng = random.Random(spec.seed)

    if mode == "single":
        order = [spec.picks[0]]
        sizes = [spec.total_bars]
    elif mode == "multi":
        order = list(spec.picks)
        sizes = _split_bars(spec.total_bars, _normalize([1.0] * len(order)))
    elif mode == "weighted":
        order = list(spec.picks)
        sizes = _split_bars(spec.total_bars, _normalize([p.weight for p in order]))
    elif mode == "chained":
        order = list(spec.picks)
        if spec.repeat_to_fill:
            sizes = _split_bars(spec.total_bars, _normalize([p.weight for p in order]))
        else:
            sizes = _split_bars(spec.total_bars, _normalize([p.weight for p in order]))
    elif mode == "randomized":
        order = list(spec.picks)
        rng.shuffle(order)
        sizes = _split_bars(spec.total_bars, _normalize([p.weight for p in order]))
    else:
        raise ValueError(f"unknown compose mode: {spec.mode!r}")

    bars: list[Bar] = []
    timeline: list[dict] = []
    cur_price = spec.start_price
    cur_ts = spec.start_ts
    for i, (pick, n) in enumerate(zip(order, sizes)):
        seg = synth_ohlcv(
            SCENARIOS[pick.code],
            n,
            start_price=cur_price,
            start_ts=cur_ts,
            sec_per_bar=spec.sec_per_bar,
            seed=f"{spec.seed}:{i}:{pick.code}",
        )
        timeline.append({
            "code": pick.code,
            "name": SCENARIOS[pick.code].name,
            "start_idx": len(bars),
            "end_idx": len(bars) + len(seg) - 1,
            "weight": pick.weight,
        })
        bars.extend(seg)
        if seg:
            cur_price = seg[-1].close
            cur_ts = seg[-1].ts + spec.sec_per_bar
    return ComposedSeries(bars=bars, timeline=timeline, seed=spec.seed)
