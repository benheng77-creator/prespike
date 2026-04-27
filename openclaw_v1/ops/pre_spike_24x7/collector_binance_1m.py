from __future__ import annotations

import argparse, datetime as dt, json, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "ops" / "pre_spike_24x7"
DATA = BASE / "data" / "binance_1m"
LOGS = BASE / "logs"
STATE = BASE / "state"

SYMBOLS = [
    "ADAUSDT","ARBUSDT","AVAXUSDT","BCHUSDT","BNBUSDT","BONKUSDT",
    "BTCUSDT","DOGEUSDT","DOTUSDT","ETHUSDT","INJUSDT","LINKUSDT",
    "LTCUSDT","OPUSDT","PEPEUSDT","SEIUSDT","SOLUSDT","SUIUSDT",
    "TRXUSDT","WIFUSDT","XRPUSDT"
]

INTERVAL_SECONDS = 20

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def log(event: dict) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    event["ts_iso"] = now_iso()
    p = LOGS / f"collector_{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')}.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":"), default=str) + "\n")

def last_ts(symbol: str) -> int | None:
    p = DATA / f"{symbol}.jsonl"
    if not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, 8192)
            f.seek(-block, 2)
            lines = f.read().decode("utf-8", errors="ignore").strip().splitlines()
        for line in reversed(lines):
            if line.strip():
                return int(json.loads(line)["ts_ms"])
    except Exception:
        return None
    return None

def fetch_closed(symbol: str) -> list:
    q = urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": 3})
    url = f"https://api.binance.com/api/v3/klines?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw-PreSpike-MacCollector/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read().decode("utf-8"))
    now_ms = int(time.time() * 1000)
    closed = [x for x in rows if int(x[6]) < now_ms - 1000]
    return closed[-1] if closed else rows[-2]

def append(symbol: str, row: list) -> bool:
    DATA.mkdir(parents=True, exist_ok=True)
    ts = int(row[0])
    old = last_ts(symbol)
    if old is not None and ts <= old:
        return False
    item = {
        "exchange": "binance",
        "symbol": symbol,
        "bar": "1m",
        "ts_ms": ts,
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
        "close_time_ms": int(row[6]),
        "fetched_ts_iso": now_iso(),
    }
    with open(DATA / f"{symbol}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(item, separators=(",", ":")) + "\n")
    return True

def cycle() -> None:
    ok = 0
    new = 0
    errors = []
    for sym in SYMBOLS:
        try:
            row = fetch_closed(sym)
            new += int(append(sym, row))
            ok += 1
        except Exception as e:
            errors.append({"symbol": sym, "error": f"{type(e).__name__}: {e}"})
    STATE.mkdir(parents=True, exist_ok=True)
    hb = {"ts_iso": now_iso(), "ok_count": ok, "new_count": new, "error_count": len(errors), "errors": errors[:10]}
    (STATE / "collector_heartbeat.json").write_text(json.dumps(hb, indent=2), encoding="utf-8")
    log({"event": "collector_cycle", **hb})

def preflight() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    row = fetch_closed("BTCUSDT")
    if not row or len(row) < 7:
        raise SystemExit("COLLECTOR_PREFLIGHT_FAIL")
    log({"event": "collector_preflight_pass", "sample_symbol": "BTCUSDT", "sample_ts_ms": int(row[0])})
    print("COLLECTOR_PREFLIGHT_PASS")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    preflight()
    if args.preflight:
        return
    if args.once:
        cycle()
        print("COLLECTOR_ONCE_PASS")
        return
    log({"event": "collector_start", "symbols": SYMBOLS, "interval_seconds": INTERVAL_SECONDS})
    while True:
        try:
            cycle()
        except Exception as e:
            log({"event": "collector_uncaught_error", "error": f"{type(e).__name__}: {e}"})
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
