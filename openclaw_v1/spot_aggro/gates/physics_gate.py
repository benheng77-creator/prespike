"""
L1 — Trade Physics Gate (SPOT AGGRO only).

Implements the per-trade physics gate from TRUTH_FIRST_UPGRADE_PROMPT.md §L1.

Contract (non-negotiable):
    A trade is REJECTED unless
        expected_move_bp > k * round_trip_cost_bp       (k = 2.0 default)
        notional_usd     >= min_viable_notional         (friction floor)
        notional_usd     >= exchange_min_notional       (exchange rule)
        expected_move_bp is known and > 0               (unknown = REJECT)
    where
        round_trip_cost_bp = 2 * (fee_bp + half_spread_bp + slippage_bp)

Explicitly PROHIBITED (operator directive):
    - No account-equity check.
    - No working-capital check.
    - No "minimum balance" rule.
    - No cross-reference to the L0 capital advisory.
    Rejection here is ALWAYS about the individual trade's economics vs its
    own friction — never about how much money is in the account.

The gate is pure: identical inputs produce identical output. It has no
I/O, no clock, no network. The engine supplies observed spread/slippage if
available; otherwise priors from physics.yml apply.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError as _exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required for the L1 physics gate") from _exc

log = logging.getLogger("spot_aggro.gate.l1")

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "physics.yml"
)

REQUIRED_KEYS = (
    "schema_version",
    "engine",
    "required_edge_multiple",
    "default_slippage_bp",
    "default_fee_bp_taker",
    "default_fee_bp_maker",
    "exchange_min_notional_usd",
)

VERDICT_PASS = "PASS"
VERDICT_REJECT = "REJECT"

# Reason codes — stable identifiers for the rejection_log (Phase 5) and
# dashboards. Never reuse a code for a different reason.
REJ_UNKNOWN_EXPECTED_MOVE = "PHY-001"     # expected_move_bp is None / <= 0
REJ_EDGE_BELOW_MULTIPLE  = "PHY-002"      # expected_move_bp <= k * cost
REJ_NOTIONAL_BELOW_FLOOR = "PHY-003"      # notional < friction-derived floor
REJ_NOTIONAL_BELOW_EXCH  = "PHY-004"      # notional < exchange minimum
REJ_INVALID_INPUT        = "PHY-005"      # programmer error — negative inputs etc.


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SymbolPrior:
    half_spread_bp: float
    slippage_bp: float
    fee_bp_taker: float
    fee_bp_maker: float


@dataclass(frozen=True)
class PhysicsConfig:
    schema_version: str
    engine: str
    required_edge_multiple: float
    default_slippage_bp: float
    default_fee_bp_taker: float
    default_fee_bp_maker: float
    post_only_preferred: bool
    exchange_min_notional_usd: float
    symbol_priors: dict[str, SymbolPrior]

    @staticmethod
    def load(path: Optional[Path] = None) -> "PhysicsConfig":
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            raise FileNotFoundError(f"L1 physics config not found: {cfg_path}")
        with cfg_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        missing = [k for k in REQUIRED_KEYS if k not in raw]
        if missing:
            raise ValueError(
                f"L1 physics config {cfg_path} missing required keys: {missing}"
            )
        if raw["engine"] != "spot_aggro":
            raise ValueError(
                f"L1 physics config engine must be 'spot_aggro' (got {raw['engine']!r})."
            )

        default_taker = float(raw["default_fee_bp_taker"])
        default_maker = float(raw["default_fee_bp_maker"])
        default_slip = float(raw["default_slippage_bp"])

        priors: dict[str, SymbolPrior] = {}
        for sym, row in (raw.get("symbols") or {}).items():
            priors[str(sym)] = SymbolPrior(
                half_spread_bp=float(row.get("half_spread_bp", 3.0)),
                slippage_bp=float(row.get("slippage_bp", default_slip)),
                fee_bp_taker=float(row.get("fee_bp_taker", default_taker)),
                fee_bp_maker=float(row.get("fee_bp_maker", default_maker)),
            )

        return PhysicsConfig(
            schema_version=str(raw["schema_version"]),
            engine=str(raw["engine"]),
            required_edge_multiple=float(raw["required_edge_multiple"]),
            default_slippage_bp=default_slip,
            default_fee_bp_taker=default_taker,
            default_fee_bp_maker=default_maker,
            post_only_preferred=bool(raw.get("post_only_preferred", True)),
            exchange_min_notional_usd=float(raw["exchange_min_notional_usd"]),
            symbol_priors=priors,
        )

    def prior_for(self, symbol: str) -> SymbolPrior:
        if symbol in self.symbol_priors:
            return self.symbol_priors[symbol]
        return SymbolPrior(
            half_spread_bp=3.0,
            slippage_bp=self.default_slippage_bp,
            fee_bp_taker=self.default_fee_bp_taker,
            fee_bp_maker=self.default_fee_bp_maker,
        )


# ---------------------------------------------------------------------------
# Decision type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PhysicsDecision:
    verdict: str                # "PASS" | "REJECT"
    reason_code: Optional[str]  # None on PASS; stable PHY-### on REJECT
    symbol: str
    notional_usd: float
    expected_move_bp: Optional[float]
    round_trip_cost_bp: float
    min_viable_notional_usd: float
    edge_multiple_actual: Optional[float]   # expected_move / cost, None if unknown
    required_edge_multiple: float
    components: dict[str, float]            # fee_bp, half_spread_bp, slippage_bp, ...
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Pure physics arithmetic
# ---------------------------------------------------------------------------

def compute_round_trip_cost_bp(
    fee_bp: float,
    half_spread_bp: float,
    slippage_bp: float,
) -> float:
    """Symmetric two-leg cost: enter and exit pay fee + half_spread + slippage.

    round_trip = 2 * (fee_bp + half_spread_bp + slippage_bp)
    """
    for name, v in (
        ("fee_bp", fee_bp),
        ("half_spread_bp", half_spread_bp),
        ("slippage_bp", slippage_bp),
    ):
        if v is None or v < 0:
            raise ValueError(f"{name} must be a non-negative number, got {v!r}")
    return 2.0 * (float(fee_bp) + float(half_spread_bp) + float(slippage_bp))


def compute_min_viable_notional(
    round_trip_cost_bp: float,
    expected_move_bp: float,
    required_edge_multiple: float,
    exchange_min_notional_usd: float,
) -> float:
    """Minimum notional for which friction does not dominate the expected edge.

    Derivation: for a trade of notional N
        absolute_friction_usd = N * (round_trip_cost_bp / 1e4)
        absolute_expected_pnl = N * (expected_move_bp  / 1e4)

    Requiring expected_pnl >= k * friction at ANY notional only constrains bp
    values (cancels N). But we also need a DOLLAR floor on PnL to survive
    rounding / discrete fills. The spec's floor is:

        cost_floor_usd = absolute_friction_usd
        min_notional   = cost_floor_usd / expected_move_pct
                       = round_trip_cost_bp / expected_move_bp  (dimensionless)
                         * (implicit N) ... which collapses.

    The operationally meaningful reading: the trade must be large enough
    that one bp of expected move produces at least one bp of friction cover.
    In practice this reduces to "notional >= exchange min" once the bp
    inequality holds. We therefore return:

        max(
            exchange_min_notional_usd,
            round_trip_cost_bp / max(expected_move_bp, epsilon)
              * exchange_min_notional_usd
        )

    i.e. scale exchange min up when the expected move is smaller than the
    cost (edge multiple < 1), so a barely-positive-edge trade doesn't sneak
    through at the exchange minimum. If edge_multiple >= 1, the floor is
    just the exchange minimum.
    """
    if expected_move_bp is None or expected_move_bp <= 0:
        raise ValueError("expected_move_bp must be > 0 to compute a notional floor")
    if round_trip_cost_bp < 0:
        raise ValueError("round_trip_cost_bp must be >= 0")
    if exchange_min_notional_usd < 0:
        raise ValueError("exchange_min_notional_usd must be >= 0")

    if expected_move_bp >= round_trip_cost_bp:
        scale = 1.0
    else:
        scale = round_trip_cost_bp / expected_move_bp

    return round(exchange_min_notional_usd * scale, 6)


def _fee_bp_for(
    cfg: PhysicsConfig,
    prior: SymbolPrior,
    *,
    post_only: Optional[bool],
) -> float:
    """Pick maker vs taker fee. Uses config default for post-only intent if
    the caller doesn't pass one."""
    if post_only is None:
        post_only = cfg.post_only_preferred
    return prior.fee_bp_maker if post_only else prior.fee_bp_taker


# ---------------------------------------------------------------------------
# Gate class
# ---------------------------------------------------------------------------

class TradePhysicsGate:
    """L1 — per-trade physics gate. Never consults account equity.

    Usage (Phase 8 wiring — not live yet):
        gate = TradePhysicsGate()
        d = gate.check(
            symbol="INJ-USDT",
            notional_usd=20.0,
            expected_move_bp=80.0,
            observed_half_spread_bp=3.2,
            observed_slippage_bp=4.0,
            post_only=True,
        )
        if d.verdict != "PASS":
            rejection_log.write(d)
            return
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._cfg = PhysicsConfig.load(self._config_path)

    @property
    def config(self) -> PhysicsConfig:
        return self._cfg

    def reload_config(self) -> PhysicsConfig:
        self._cfg = PhysicsConfig.load(self._config_path)
        return self._cfg

    # -- Exposed helpers (for engine + tests) --------------------------------

    def round_trip_cost_bp_for(
        self,
        symbol: str,
        *,
        observed_half_spread_bp: Optional[float] = None,
        observed_slippage_bp: Optional[float] = None,
        post_only: Optional[bool] = None,
    ) -> float:
        prior = self._cfg.prior_for(symbol)
        half = observed_half_spread_bp if observed_half_spread_bp is not None else prior.half_spread_bp
        slip = observed_slippage_bp if observed_slippage_bp is not None else prior.slippage_bp
        fee = _fee_bp_for(self._cfg, prior, post_only=post_only)
        return compute_round_trip_cost_bp(fee, half, slip)

    def min_viable_notional_for(
        self,
        symbol: str,
        expected_move_bp: float,
        *,
        observed_half_spread_bp: Optional[float] = None,
        observed_slippage_bp: Optional[float] = None,
        post_only: Optional[bool] = None,
    ) -> float:
        cost = self.round_trip_cost_bp_for(
            symbol,
            observed_half_spread_bp=observed_half_spread_bp,
            observed_slippage_bp=observed_slippage_bp,
            post_only=post_only,
        )
        return compute_min_viable_notional(
            round_trip_cost_bp=cost,
            expected_move_bp=expected_move_bp,
            required_edge_multiple=self._cfg.required_edge_multiple,
            exchange_min_notional_usd=self._cfg.exchange_min_notional_usd,
        )

    # -- Main entry point ----------------------------------------------------

    def check(
        self,
        *,
        symbol: str,
        notional_usd: float,
        expected_move_bp: Optional[float],
        observed_half_spread_bp: Optional[float] = None,
        observed_slippage_bp: Optional[float] = None,
        post_only: Optional[bool] = None,
    ) -> PhysicsDecision:
        """Evaluate one proposed trade. Pure function — no I/O, no clock.

        `expected_move_bp is None` or `<= 0` → REJECT (spec: unknown = REJECT).
        """

        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a non-empty string")
        if notional_usd is None or notional_usd < 0:
            return self._reject(
                REJ_INVALID_INPUT,
                f"notional_usd must be >= 0, got {notional_usd!r}",
                symbol=symbol,
                notional_usd=notional_usd if notional_usd is not None else 0.0,
                expected_move_bp=expected_move_bp,
                components={},
                round_trip_cost_bp=0.0,
                min_viable_notional_usd=0.0,
                edge_multiple_actual=None,
            )

        prior = self._cfg.prior_for(symbol)
        half = observed_half_spread_bp if observed_half_spread_bp is not None else prior.half_spread_bp
        slip = observed_slippage_bp if observed_slippage_bp is not None else prior.slippage_bp
        fee = _fee_bp_for(self._cfg, prior, post_only=post_only)

        try:
            cost_bp = compute_round_trip_cost_bp(fee, half, slip)
        except ValueError as exc:
            return self._reject(
                REJ_INVALID_INPUT,
                str(exc),
                symbol=symbol,
                notional_usd=float(notional_usd),
                expected_move_bp=expected_move_bp,
                components={"fee_bp": fee, "half_spread_bp": half, "slippage_bp": slip},
                round_trip_cost_bp=0.0,
                min_viable_notional_usd=0.0,
                edge_multiple_actual=None,
            )

        # Unknown expected move → REJECT (spec §L1)
        if expected_move_bp is None or expected_move_bp <= 0:
            return self._reject(
                REJ_UNKNOWN_EXPECTED_MOVE,
                "expected_move_bp is unknown or non-positive; "
                "spec §L1 treats unknown horizon as REJECT",
                symbol=symbol,
                notional_usd=float(notional_usd),
                expected_move_bp=expected_move_bp,
                components={
                    "fee_bp": fee,
                    "half_spread_bp": half,
                    "slippage_bp": slip,
                },
                round_trip_cost_bp=cost_bp,
                min_viable_notional_usd=self._cfg.exchange_min_notional_usd,
                edge_multiple_actual=None,
            )

        edge_multiple = expected_move_bp / cost_bp if cost_bp > 0 else float("inf")
        min_notional = compute_min_viable_notional(
            round_trip_cost_bp=cost_bp,
            expected_move_bp=float(expected_move_bp),
            required_edge_multiple=self._cfg.required_edge_multiple,
            exchange_min_notional_usd=self._cfg.exchange_min_notional_usd,
        )

        components = {
            "fee_bp": fee,
            "half_spread_bp": half,
            "slippage_bp": slip,
            "cost_bp": cost_bp,
            "expected_move_bp": float(expected_move_bp),
            "k_required": self._cfg.required_edge_multiple,
        }

        # Edge check: expected_move_bp must STRICTLY exceed k * cost.
        threshold = self._cfg.required_edge_multiple * cost_bp
        if expected_move_bp <= threshold:
            return self._reject(
                REJ_EDGE_BELOW_MULTIPLE,
                f"expected_move_bp={expected_move_bp:.2f} <= k*cost="
                f"{threshold:.2f} (k={self._cfg.required_edge_multiple}, "
                f"cost_bp={cost_bp:.2f})",
                symbol=symbol,
                notional_usd=float(notional_usd),
                expected_move_bp=float(expected_move_bp),
                components=components,
                round_trip_cost_bp=cost_bp,
                min_viable_notional_usd=min_notional,
                edge_multiple_actual=edge_multiple,
            )

        # Notional floors: exchange first, then friction-scaled.
        if notional_usd < self._cfg.exchange_min_notional_usd:
            return self._reject(
                REJ_NOTIONAL_BELOW_EXCH,
                f"notional=${notional_usd:.4f} below exchange_min="
                f"${self._cfg.exchange_min_notional_usd:.4f}",
                symbol=symbol,
                notional_usd=float(notional_usd),
                expected_move_bp=float(expected_move_bp),
                components=components,
                round_trip_cost_bp=cost_bp,
                min_viable_notional_usd=min_notional,
                edge_multiple_actual=edge_multiple,
            )

        if notional_usd < min_notional:
            return self._reject(
                REJ_NOTIONAL_BELOW_FLOOR,
                f"notional=${notional_usd:.4f} below min_viable="
                f"${min_notional:.4f} (scaled by cost/expected ratio)",
                symbol=symbol,
                notional_usd=float(notional_usd),
                expected_move_bp=float(expected_move_bp),
                components=components,
                round_trip_cost_bp=cost_bp,
                min_viable_notional_usd=min_notional,
                edge_multiple_actual=edge_multiple,
            )

        return PhysicsDecision(
            verdict=VERDICT_PASS,
            reason_code=None,
            symbol=symbol,
            notional_usd=float(notional_usd),
            expected_move_bp=float(expected_move_bp),
            round_trip_cost_bp=cost_bp,
            min_viable_notional_usd=min_notional,
            edge_multiple_actual=edge_multiple,
            required_edge_multiple=self._cfg.required_edge_multiple,
            components=components,
            reason=(
                f"expected_move_bp={expected_move_bp:.2f} > "
                f"{self._cfg.required_edge_multiple}*cost_bp="
                f"{threshold:.2f}; notional=${notional_usd:.2f} >= "
                f"min_viable=${min_notional:.2f}"
            ),
        )

    # -- Internal ------------------------------------------------------------

    def _reject(
        self,
        code: str,
        message: str,
        *,
        symbol: str,
        notional_usd: float,
        expected_move_bp: Optional[float],
        components: dict[str, float],
        round_trip_cost_bp: float,
        min_viable_notional_usd: float,
        edge_multiple_actual: Optional[float],
    ) -> PhysicsDecision:
        d = PhysicsDecision(
            verdict=VERDICT_REJECT,
            reason_code=code,
            symbol=symbol,
            notional_usd=float(notional_usd),
            expected_move_bp=expected_move_bp,
            round_trip_cost_bp=round_trip_cost_bp,
            min_viable_notional_usd=min_viable_notional_usd,
            edge_multiple_actual=edge_multiple_actual,
            required_edge_multiple=self._cfg.required_edge_multiple,
            components=components,
            reason=f"[{code}] {message}",
        )
        log.info(
            "[L1] REJECT %s notional=$%.4f expected=%s cost=%.2fbp min=$%.4f %s",
            symbol,
            float(notional_usd),
            f"{expected_move_bp:.2f}bp" if expected_move_bp is not None else "None",
            round_trip_cost_bp,
            min_viable_notional_usd,
            code,
        )
        return d


# ---------------------------------------------------------------------------
# CLI helper for manual evaluation
# ---------------------------------------------------------------------------

def _main() -> int:  # pragma: no cover
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="spot_aggro.gates.physics_gate",
        description="Evaluate a single proposed trade against L1 physics.",
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--notional", type=float, required=True)
    parser.add_argument("--expected-move-bp", type=float, required=True)
    parser.add_argument("--half-spread-bp", type=float, default=None)
    parser.add_argument("--slippage-bp", type=float, default=None)
    parser.add_argument("--taker", action="store_true", help="Assume taker fee")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    gate = TradePhysicsGate()
    d = gate.check(
        symbol=args.symbol,
        notional_usd=args.notional,
        expected_move_bp=args.expected_move_bp,
        observed_half_spread_bp=args.half_spread_bp,
        observed_slippage_bp=args.slippage_bp,
        post_only=None if not args.taker else False,
    )
    if args.json:
        print(json.dumps(d.to_dict(), indent=2, default=str))
    else:
        print(f"verdict:    {d.verdict}   (code={d.reason_code or '-'})")
        print(f"symbol:     {d.symbol}")
        print(f"notional:   ${d.notional_usd:.4f}")
        print(f"expected:   {d.expected_move_bp}bp")
        print(f"cost:       {d.round_trip_cost_bp:.2f}bp")
        print(f"edge_x:     {d.edge_multiple_actual}")
        print(f"min_notnl:  ${d.min_viable_notional_usd:.4f}")
        print(f"reason:     {d.reason}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
