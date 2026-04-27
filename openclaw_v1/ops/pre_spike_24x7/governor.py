from __future__ import annotations

import argparse, datetime as dt, json, subprocess, time
from pathlib import Path

ROOT = Path("/Users/Admin/LIVE-projects/prespike/openclaw_v1")
BASE = ROOT / "ops" / "pre_spike_24x7"
PYTHON = Path("/Library/Frameworks/Python.framework/Versions/3.11/bin/python3")
COLLECTOR = BASE / "collector_binance_1m.py"
SCANNER = BASE / "scanner_live_lightgbm.py"
PIDS = BASE / "pids"
LOGS = BASE / "logs"
STATE = BASE / "state"
DATA = BASE / "data" / "binance_1m"
STALE_SECONDS = 180

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def log(event: dict) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    event["ts_iso"] = now_iso()
    p = LOGS / f"governor_{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')}.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":"), default=str) + "\n")

def pid_alive(pid: int) -> bool:
    try:
        subprocess.run(["kill", "-0", str(pid)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def start(name: str, script: Path) -> int:
    PIDS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    out = open(LOGS / f"{name}_process.log", "a", encoding="utf-8")
    proc = subprocess.Popen([str(PYTHON), str(script)], cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT)
    (PIDS / f"{name}.pid").write_text(str(proc.pid), encoding="utf-8")
    log({"event": "process_started", "name": name, "pid": proc.pid})
    return proc.pid

def ensure_process(name: str, script: Path) -> None:
    pf = PIDS / f"{name}.pid"
    if pf.exists():
        try:
            pid = int(pf.read_text(encoding="utf-8").strip())
            if pid_alive(pid):
                log({"event": "process_alive", "name": name, "pid": pid})
                return
            log({"event": "dead_pid_detected", "name": name, "pid": pid})
        except Exception as e:
            log({"event": "pid_read_error", "name": name, "error": str(e)})
    start(name, script)

def heartbeat(name: str) -> dict | None:
    p = STATE / f"{name}_heartbeat.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def heartbeat_age(name: str) -> float | None:
    hb = heartbeat(name)
    if not hb or "ts_iso" not in hb:
        return None
    try:
        ts = dt.datetime.fromisoformat(hb["ts_iso"])
        return (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
    except Exception:
        return None

def kill_pid(name: str) -> None:
    pf = PIDS / f"{name}.pid"
    if not pf.exists():
        return
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
        subprocess.run(["kill", "-9", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log({"event": "process_killed", "name": name, "pid": pid})
    except Exception as e:
        log({"event": "kill_error", "name": name, "error": str(e)})
    try:
        pf.unlink()
    except Exception:
        pass

def data_quality() -> None:
    files = sorted(DATA.glob("*.jsonl"))
    fresh = 0
    stale = []
    now_ms = int(time.time() * 1000)
    for p in files:
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                block = min(size, 8192)
                f.seek(-block, 2)
                lines = f.read().decode("utf-8", errors="ignore").strip().splitlines()
            last = json.loads(lines[-1])
            age = (now_ms - int(last["ts_ms"])) / 1000
            if age <= 180:
                fresh += 1
            else:
                stale.append({"symbol": p.stem, "age_sec": age})
        except Exception as e:
            stale.append({"symbol": p.stem, "error": str(e)})
    log({"event": "gov_layer_2_data_quality", "files": len(files), "fresh": fresh, "stale_count": len(stale), "stale": stale[:10]})

def model_quality() -> None:
    models = sorted([p for p in (ROOT / "models").glob("model_v*") if p.is_dir()], key=lambda p: p.stat().st_mtime)
    if not models:
        log({"event": "gov_layer_3_model_quality", "status": "FAIL", "reason": "no_model"})
        return
    latest = models[-1]
    status = "PASS" if (latest / "manifest.json").exists() and (latest / "lightgbm_model.txt").exists() else "FAIL"
    shb = heartbeat("scanner") or {}
    log({"event": "gov_layer_3_model_signal_quality", "status": status, "latest_model": latest.name, "observations": shb.get("observations"), "signals": shb.get("signals")})

def governance_once() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)

    ensure_process("collector", COLLECTOR)
    ensure_process("scanner", SCANNER)

    for name, script in [("collector", COLLECTOR), ("scanner", SCANNER)]:
        age = heartbeat_age(name)
        if age is not None and age > STALE_SECONDS:
            log({"event": "gov_layer_1_stale_restart", "name": name, "age_sec": age})
            kill_pid(name)
            start(name, script)

    log({"event": "gov_layer_1_process_supervisor", "collector_age_sec": heartbeat_age("collector"), "scanner_age_sec": heartbeat_age("scanner")})
    data_quality()
    model_quality()
    (STATE / "governor_heartbeat.json").write_text(json.dumps({"ts_iso": now_iso(), "status": "governor_cycle_complete"}, indent=2), encoding="utf-8")

def preflight() -> None:
    for p in [PYTHON, COLLECTOR, SCANNER]:
        if not p.exists():
            raise SystemExit(f"GOVERNOR_PREFLIGHT_FAIL missing {p}")
    subprocess.run([str(PYTHON), "-m", "py_compile", str(COLLECTOR), str(SCANNER)], check=True)
    subprocess.run([str(PYTHON), str(COLLECTOR), "--preflight"], check=True, cwd=str(ROOT))
    subprocess.run([str(PYTHON), str(SCANNER), "--preflight"], check=True, cwd=str(ROOT))
    print("GOVERNOR_PREFLIGHT_PASS")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    preflight()
    if args.preflight:
        return
    governance_once()
    print("GOVERNOR_ONCE_PASS")

if __name__ == "__main__":
    main()
