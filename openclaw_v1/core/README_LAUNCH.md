# Launch procedure — scanner, 75% accuracy gate, live trading

This is the honest launch checklist for the three upgrades shipped on
2026-04-13. Nothing here guarantees profit; all of it enforces
guardrails around the strategy you already have.

## What shipped

| Module                  | Purpose                                                                                                                                    |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `core/scanner.py`       | Multi-symbol ranker. Evaluates a universe each cycle and reports the best pair. Default off.                                               |
| `core/accuracy_gate.py` | Rolling win-rate floor + proving-period enforcement. Default ON at 75%.                                                                    |
| `core/live_executor.py` | Real-order executor with DRY-RUN-by-default and four independent safety gates. Default off.                                                |
| `core/config.py`        | New nested sections: `scanner`, `accuracy_gate`, `live_trading`. Existing fields unchanged.                                                |
| `config/openclaw.yaml`  | Added 3 new config sections with safe defaults.                                                                                            |
| `main.py`               | Removed the hard refuse-live error (which blocked launch). Added gate + executor wiring in `decision_loop`. Existing paper flow identical. |

## How the three features interact

```
 decision_engine  ──EXECUTE──▶  accuracy_gate  ──allow──▶  risk_engine (Kelly size)
                                       │
                                       ▼
                                    live_executor  ──submit──▶  exchange (real order)
                                          │
                                          ▼
                                     paper portfolio  ──record──▶  SQLite trades
                                                                    (used by gate)
```

- The **accuracy gate** reads the same `trades` table the paper portfolio
  writes to. The more the bot paper-trades, the more data the gate has,
  the more confidently it enforces the 75% floor.
- The **live executor** always runs, but only _actually submits_ to the
  exchange when all four flags align (see below). Otherwise it logs a
  dry-run line and the paper ledger continues as before.
- The **scanner** is currently a recommender — it tells you which pair
  looks best today. Auto-trading the scanner's winner requires per-symbol
  tracker isolation, which is a separate upgrade.

## Launch procedure — strict step order

**Step 0 — baseline (already true).**

- `config.mode = paper` in `config/openclaw.yaml`.
- Bot is running, writing decisions + paper trades to `logs/openclaw.db`.
- Your current record in the DB is what the accuracy gate will use to
  decide whether you're allowed to go live.

**Step 1 — validate the accuracy gate.**
Check the current rolling WR:

```bash
python - <<'PY'
import sqlite3
c = sqlite3.connect('logs/openclaw.db')
for sym, n, w in c.execute("""
  SELECT symbol, COUNT(*), SUM(CASE WHEN pnl_r>0 THEN 1 ELSE 0 END)
  FROM trades WHERE status<>'open' GROUP BY symbol
"""):
  wr = 100*w/n if n else 0
  print(f"{sym}: {n} trades, {w} wins, {wr:.1f}%")
PY
```

If any symbol is under 75% and has >= 10 trades, the gate will veto new
EXECUTEs on that symbol. This is the point.

**Step 2 — turn the scanner on in recommend mode.**
In `config/openclaw.yaml`:

```yaml
scanner:
  enabled: true
  mode: recommend # does NOT trade the winner, only logs
  universe:
    - BTC/USDT
    - ETH/USDT
    - SOL/USDT
    - APT/USDT
```

Run the bot; watch `logs/apt_live.log` or the panel for scanner ranking
lines. This tells you the daily best pair without committing any capital.

**Step 3 — shrink the position size cap.**

```yaml
live_trading:
  max_notional_quote: 50.0 # start here; raise only after clean fills
  max_trades_per_day: 5
```

The first live orders should be tiny. $50 is an order of magnitude below
your paper starting balance ($10,000) — losing all 5 daily trades at
$50 each caps worst-case loss at ~$50 × 1R × 5 = $250 per day even in
a catastrophic scenario. You can raise this later.

**Step 4 — the four independent live gates (must ALL align).**
Real orders only fire when every one of these is true simultaneously:

| Gate                          | Where                  | Default   |
| ----------------------------- | ---------------------- | --------- |
| `config.mode`                 | `config/openclaw.yaml` | `paper`   |
| `config.live_trading.enabled` | `config/openclaw.yaml` | `false`   |
| env `OPENCLAW_LIVE_TRADING`   | process env            | unset     |
| `cache/.halt` absent          | filesystem             | may exist |

Flipping ONE of these is not enough. The design is deliberate — you
cannot accidentally push live orders by editing one config field.

**Step 5 — go live.**

```yaml
mode: live
live_trading:
  enabled: true
```

Then in the shell:

```bash
export OPENCLAW_LIVE_TRADING=1
rm -f cache/.halt                   # clear the halt file
python main.py
```

Watch the log for lines like `LIVE ORDER OUT:` (submitted) or
`executor dry-run:` (dry-run; one or more gates still closed).

**Step 6 — monitor reconciliation.**
Every real order is followed by a `fetch_order` loop that compares
requested size vs. filled size. If the mismatch exceeds 1%, the
executor writes the halt file itself and refuses further live orders
until you investigate and manually clear it.

**Step 7 — pause any time.**

```bash
touch cache/.halt                   # or click ■ Halt trading in the panel
```

Pause affects live and paper uniformly.

## What the 75% accuracy gate actually enforces

Per-symbol, on each EXECUTE attempt, it queries the last `window_size`
(default 30) closed trades for that symbol from `logs/openclaw.db`:

- If `window_trades < min_trades_before_floor` (default 10), **allow**
  — not enough data to enforce a floor, paper mode keeps collecting
  samples.
- If `window_wins / window_trades < floor_pct` (default 0.75), **veto**
  — the bot has been losing too often lately; take new entries off the
  table for this symbol and latch a `cooldown_bars` cooldown.
- If `config.mode == live` and this symbol has fewer than `proving_wins`
  lifetime wins, **veto** — a symbol must prove itself in paper first.

**Honest note on 75%.** No code can force the market to give you 75%
accuracy. This gate only refuses to keep trading when you don't have
it. To _achieve_ 75%, you still have to retrain the conviction model,
tighten the score thresholds, and validate on out-of-sample data. The
gate is a seatbelt, not an engine.

## What the scanner does NOT do yet

- It does not auto-swap trading symbols. In `recommend` mode it only
  logs ranking. `auto` mode is defined in the config but the per-symbol
  feature trackers (FlowTracker, OrderBookTracker, FundingTracker,
  OpenInterestTracker) aren't isolated per symbol yet — trading
  multiple pairs concurrently would mix their state.
- It does not re-warm a new symbol's bars on swap. Adding that is the
  next step when you want true autonomous pair selection.

## Panel visibility

The control panel already shows:

- Halt status (green/amber).
- Open positions with full trade detail.
- Today's closed trades with wins/losses.
- Rolling 14-day P&L.

A follow-up panel card can show:

- Scanner ranking per cycle.
- Accuracy gate decision per symbol.
- Live executor status (which of the four gates are open).

Those endpoints are not yet wired — propose them as the next panel
upgrade once you've run the bot with the new modules for a day and
have real data to display.
