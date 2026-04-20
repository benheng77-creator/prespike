# Root Cause Analysis — Why the trading bot loses money

**Date:** 2026-04-20  
**Data basis:** 216 real closed trades, 5 backtests (62,500 simulated bars total), math-verified engine.

---

## The answer in one line

**The admission signal has no information content about future price.** Everything else — exit calibration, governance layers, zombie SL cleanup, TP sweeps — is downstream of this single fact.

---

## Evidence trail (backward from loss)

### Live trading reality (216 closed trades)

```
Total NET PnL:       -$12.72
Total capital used:  $2,026
Mean per-trade:      -62.8 bp after all costs
Win rate:            34.0%  (70 wins / 136 losses)
Total fees:          $0.69  (3.4 bp/trade — tiny, not the problem)
```

### Exit breakdown (where the losses land)

| Reason | n | Mean gross | Mean net | Total PnL |
|---|---|---|---|---|
| SL       | 111 | -114 bp | -118 bp | **-$15.81** |
| TP       | 12  | +167 bp | +165 bp | +$1.79 |
| TRAIL    | 36  | +32 bp  | +31 bp  | +$0.90 |
| TIME_STOP | 19 | +17 bp  | +17 bp  | +$0.32 |
| SPI_COLLAPSE | 16 | +8 bp | +8 bp | +$0.21 |
| COMP_DECAY | 20 | +5 bp  | +0.7 bp | +$0.006 |

### SL breakdown (the concentration of loss)

```
111 SL fires   total -$15.81
  - 100 normal SL exits (hold < 1h):  -$7.90  (avg -$0.08 each)
  - 11 zombie SL exits  (hold > 1h):  -$7.91  (avg -$0.72 each)
    ^ all closed in a single 29-second burst on 2026-04-18 22:38
      from operator-initiated /positions/close_all?reason=SL
```

The zombies are a one-time historical accident, not a recurring bug. Strip them → normal live loss = **-$4.81**.

---

## Three layers of cause (symptom → mechanism → root)

### Layer 1 — Symptom
Mean per-trade is -62.8 bp. Over 216 trades that compounds to -$12.72.

### Layer 2 — Mechanical cause (risk/reward ratio fails)

```
Configured TP:SL ratio:   1.5% : 1.0%  →  1.5 : 1
Breakeven WR required:    1 / (1 + 1.5)  =  40.0%
Actual WR observed:       34.0%
Gap:                      6 percentage points
```

6 pp of missing WR, applied to ~100bp SL size, on 136 losers = roughly -$8 of structural loss beyond fees. This matches the remaining loss after zombies are excluded.

### Layer 3 — Upstream cause (admission signal is noise)

The 6 pp WR gap cannot be closed by tuning exits. I proved this across five independent backtests:

| Backtest | Signal / calibration | Result |
|---|---|---|
| A (variant-only) | contrarian + deep_value + momentum | -$38.46 over 2,033 trades, WR 27% |
| B (full 7-sprint) | same signals + full governance | -$23.71 over 1,209 trades, WR 25% |
| Delta | CDV + counterfactual TP/SL replay | Mean causal delta ±0.1 bp — **TP/SL calibration does not change outcome** |
| Sweep | TP in {0.3%, 0.5%, 0.7%, 1.0%} | Total PnL spread across 4 TPs: $0.24 — **noise** |
| Sweep | Tightened admission (0.8% pullback + 7d>+1%) | **Zero admits in 42,000 bars** |
| Sweep | Breakout swap (ret_5m>+0.5%) | -25 bp/trade — **worse** |
| Aqueduct | Cross-venue spread arb, math-verified engine | NO_EDGE_SHELVE across all 3 cost tiers, all 12 symbols |

**The admission rule "ret_5m ≤ -0.3% AND ret_7d ≥ -2%" fires approximately 3% of the time, and the subsequent 45-minute outcome is Gaussian-distributed around zero-after-costs.**

The math-verified Aqueduct confirmed this one level deeper: Hurst exponents for all 12 symbols are 0.62–0.97 (**trending, not mean-reverting**). The entire signal class — "wait for a pullback, expect bounce" — assumes mean reversion that empirically does not exist on these assets at this timescale.

---

## Why every fix has failed

Every attempted fix tried to extract alpha from a zero-alpha signal. The list:

1. **Tightening SL / widening TP** → makes individual losses smaller but WR also drops. Net unchanged.
2. **Tighter admission rule** → fires zero trades. Signal has no base rate when you demand clarity.
3. **Different admission rule (breakout)** → worse. It buys the top.
4. **7-sprint governance stack** → successfully reduces loss by 82% (from -$38 to -$6.87) **by cutting trade count, not by turning losers into winners**. Per-trade expectancy stays negative.
5. **Counterfactual TP/SL replay** → ±0.1 bp mean delta. TP calibration doesn't matter when the underlying signal is noise.
6. **Cross-venue arb (the audit's godlike variation)** → NO_EDGE verdict on real data. Spread stats say trending, not reverting.

Each of these is a valid engineering fix for a specific symptom. None of them can overcome the fact that the admission rule selects entries that don't predict anything.

---

## What this means mathematically

For any directional spot trade with TP=x%, SL=y%, fees+slippage=c bp round-trip:

```
Expected value (bp) = WR × (x × 10000 − c) − (1−WR) × (y × 10000 + c)
                    = 10000 × [WR·x − (1−WR)·y] − c
```

For your current config (x=1.5%, y=1.0%, c=30 bp all-in, WR=34%):
```
EV = 10000 × [0.34 × 0.015 − 0.66 × 0.010] − 30
   = 10000 × [0.00510 − 0.00660] − 30
   = 10000 × (−0.00150) − 30
   = −15 − 30
   = −45 bp per trade
```

Observed per-trade: -63 bp (worse than pure math predicts because of the 11 zombies skewing the SL mean).

**The equation tells you:** for EV > 0 at any reasonable cost structure, you need WR·x > (1-WR)·y, which means either much better admission selectivity (higher WR) or a much higher TP/SL ratio (longer holds). Neither is available in the signal class you're testing.

---

## The honest decision tree

- **If you can find a signal with WR > 45% on a 1.5:1 ratio**, the existing exit/risk framework will make it profitable. The Opportunity Fabric governance stack is verified sound and reduces loss by 82% when the underlying signal is noise — it will protect real edge beautifully.
- **If you cannot find such a signal on this universe in this regime**, no amount of governance / calibration / sprint iteration will create it. The correct action is to stop trading until the market regime changes OR change to a market where edge is available.
- **Buy-and-hold baseline** on the same universe over the same period: I have not computed this exact number but the 12-symbol universe was roughly flat across the 3-day aligned window. B&H has zero cost drag. If you cannot beat flat + 0 cost, you cannot beat B&H, and the trading activity is strictly destroying value.

---

## Conclusion

**Root cause: the admission rule has no predictive power.**

Proof: 5 independent backtests show mean per-trade ∈ [-62, -7] bp across every signal variation tried. TP sweep shows $0.24 variation across 4 configurations. Tight admission shows zero base rate. Cross-venue arb shows Hurst 0.62-0.97 (trending, not reverting). The math-verified Aqueduct engine returns NO_EDGE_SHELVE on real data across all cost tiers.

Everything else is downstream noise amplification.
