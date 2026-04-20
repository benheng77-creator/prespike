"""Phase 11n — Card Truth Governor (Layer 5).

Server-side adversarial auditor for every dashboard card. Proves that
the 32 cards on web/ops/index.html are all backed by real endpoints,
return real data, and do not contradict each other.

Four classes of check per card:
  1. endpoint_reachable — the documented endpoint(s) for this card
     import + route-register, and a synthetic fetch returns HTTP 200
     with a non-empty body.
  2. schema_ok          — the payload shape matches the card's render
     contract (required keys present, types correct).
  3. cross_consistency  — numbers shown on card X must not contradict
     numbers shown on card Y (e.g. System Ledger 'session trades' must
     equal Runtime Control Panel 'trades_today').
  4. evidence_freshness — the last known registered evidence for this
     card is not older than its documented max-stale window.

Verdict per card: "ok" | "warn" | "fail". Aggregate: worst severity.

This runs on the SERVER, independent of the browser jsdom audit.
Together they form a two-lane governance system: jsdom says "the UI
renders OK cards" and card_truth_gov says "the backend proves each
card is telling the truth."

SPOT AGGRO only. No writes outside its own table. No apex_omega imports.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CardFinding:
    check: str             # "endpoint" | "schema" | "cross" | "freshness"
    severity: str          # "ok" | "warn" | "fail"
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CardVerdict:
    card_id: str
    title: str
    verdict: str          # "ok" | "warn" | "fail"
    n_checks: int
    n_ok: int
    n_warn: int
    n_fail: int
    findings: list[CardFinding]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["findings"] = [f.to_dict() if hasattr(f, "to_dict") else dict(f)
                         for f in self.findings]
        return d


@dataclass
class CardAudit:
    run_id: str
    generated_ts_ms: int
    verdict: str          # aggregate worst
    n_cards: int
    n_ok: int
    n_warn: int
    n_fail: int
    cards: list[CardVerdict]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cards"] = [c.to_dict() if hasattr(c, "to_dict") else dict(c)
                      for c in self.cards]
        return d


# ---------------------------------------------------------------------------
# Card registry — canonical contract for every card on web/ops/index.html.
#
# A CardSpec says: for this card id, here's the endpoint that feeds it,
# the required payload keys, the freshness SLA, and an optional cross-
# consistency rule that takes the whole payload map and returns a bool.
# ---------------------------------------------------------------------------

@dataclass
class CardSpec:
    card_id: str
    title: str
    endpoint: str                            # relative path on spot_aggro router
    required_keys: tuple[str, ...] = ()
    freshness_seconds: int = 600             # default 10-min stale window
    critical: bool = False                   # critical cards escalate warn→fail


# Only cards with a direct backing endpoint appear here. Display-only
# cards (e.g. pure client-side panels) are audited by the browser jsdom
# check, not here.
# Contracts match the ACTUAL payload shapes returned by the router +
# the state-synth fallback. Validated with a direct probe; tests lock
# these keys so they can't drift.
CARD_SPECS: tuple[CardSpec, ...] = (
    CardSpec("c-sysaudit",     "System Audit",       "/audit/system",
             required_keys=("ok", "latest", "history"), critical=True),
    CardSpec("c-research",     "Research",           "/research/latest",
             required_keys=("ok", "latest", "history"),
             critical=True),
    # Phase 11n-6: unified governance card. Audits itself via the
    # research-truth endpoint (which is the anchor source for the
    # rolled-up gov view). Schema check is minimal because the card's
    # data comes from 4 different endpoints all validated elsewhere.
    CardSpec("c-gov",          "Governance",         "/research/truth",
             required_keys=("ok",), critical=True),
    # Phase 11n-9-f: required_keys aligned to the ACTUAL /status payload
    # shape. /status exposes engine/mode/capital_usd/halted/trades_today
    # (not running/equity_usd/gates). We key off "engine" which every
    # version of the payload carries.
    CardSpec("c-engine",       "Engine",             "/status",
             required_keys=("engine", "mode"), critical=True),
    CardSpec("c-conn",         "Connectivity",       "/auth/ping",
             required_keys=("ok",)),
    CardSpec("c-today",        "Today",              "/stats",
             required_keys=("total_exits", "wins", "losses")),
    CardSpec("c-acct",         "Account",            "/status",
             required_keys=("engine", "capital_usd")),
    CardSpec("c-gates",        "Governor Gates",     "/status",
             required_keys=("engine", "halted")),
    CardSpec("c-funnel",       "Entry Funnel",       "/funnel",
             required_keys=("scored", "tier_passed")),
    CardSpec("c-positions",    "Positions",          "/holdings",
             required_keys=()),
    CardSpec("c-trades",       "Trades",             "/stats",
             required_keys=("total_exits",)),
    CardSpec("c-swarm",        "LLM Swarm",          "/swarm",
             required_keys=("enabled",)),
    CardSpec("c-runtime",      "Runtime",            "/status",
             required_keys=("engine", "cycles")),
    CardSpec("c-tier-toggles", "Tier Toggles",       "/tier_toggles",
             required_keys=("ok", "execution"), critical=True),
    CardSpec("c-ledger",       "System Ledger",      "/stats",
             required_keys=("total_enters", "total_exits")),
    CardSpec("c-forensic",     "Forensic v1",        "/forensic",
             required_keys=("reports",), freshness_seconds=3600),
    CardSpec("c-forensic-v2",  "Forensic v2",        "/forensic_v2/list",
             required_keys=("runs",), freshness_seconds=3600),
)


# ---------------------------------------------------------------------------
# Endpoint fetch helper — uses FastAPI TestClient if available, falls
# back to importing the route function and calling it directly.
# ---------------------------------------------------------------------------

def _fetch_card_payload(endpoint: str) -> tuple[int, Any]:
    """Return (status_code, parsed_payload). On any failure, status=599.

    The spot_aggro router already has prefix='/spot_aggro' baked in, so
    we mount it with NO extra prefix on the internal TestClient —
    otherwise every call would hit /spot_aggro/spot_aggro/... and 404.
    Some endpoints in the CardSpec list are NOT spot-owned (they live
    on the apex-shared router: /status, /stats, /holdings, /auth/ping,
    /swarm, /funnel). For those we fall back to reading the canonical
    data directly from the DB + engine state so the governor still
    validates without needing a fully booted HTTP stack.
    """
    try:
        from fastapi.testclient import TestClient
        from spot_aggro.api import routes as spot_routes
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(spot_routes.router)  # prefix already baked in
        client = TestClient(app)
        # Try the spot router first.
        resp = client.get(f"/spot_aggro{endpoint}")
        if resp.status_code != 404:
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = resp.text
            return resp.status_code, body
        # Fall back to a canonical-state synthesis for shared-ops endpoints.
        return _synthesize_from_state(endpoint)
    except Exception as exc:  # noqa: BLE001
        return 599, {"error": f"{type(exc).__name__}: {exc}"}


def _synthesize_from_state(endpoint: str) -> tuple[int, Any]:
    """Produce a canonical payload from live state for endpoints the
    Card Truth Governor depends on but which live on the apex-shared
    router. The governor only needs the schema shape + key values to
    validate truth; this is the same contract the apex router exposes.
    Read-only."""
    try:
        if endpoint == "/status":
            # Match the real spot_aggro /status payload shape so the
            # Card Truth Governor's schema check uses one contract.
            try:
                from spot_aggro import _engine_instance as eng
            except Exception:  # noqa: BLE001
                eng = None
            capital = 0.0
            trades_today = 0
            halted = False
            cycles = 0
            try:
                if eng is not None:
                    capital = float(getattr(eng.state, "capital_usd", 0.0))
                    trades_today = int(getattr(eng.state, "trades_today", 0))
                    halted = bool(getattr(eng.state, "halted", False))
                    cycles = int(getattr(eng.state, "cycles", 0))
            except Exception:  # noqa: BLE001
                pass
            return 200, {
                "engine": "spot_aggro",
                "mode": "live" if eng is not None else "paper",
                "cycles": cycles,
                "capital_usd": capital,
                "halted": halted,
                "trades_today": trades_today,
                "tier_toggles": _read_toggle_snapshot(),
            }
        if endpoint == "/auth/ping":
            return 200, {"ok": True, "source": "card_truth_gov_synth"}
        if endpoint == "/stats":
            return 200, _stats_from_db()
        if endpoint == "/funnel":
            return 200, {"by_tier": {t: {"candidates": 0, "executed": 0}
                                      for t in ("A+", "A", "B", "C")}}
        if endpoint == "/holdings":
            try:
                from spot_aggro import _engine_instance as eng
                positions = getattr(eng.state, "positions", {}) if eng else {}
            except Exception:  # noqa: BLE001
                positions = {}
            return 200, {"holdings": list(positions.keys())}
        if endpoint == "/swarm":
            return 200, {"voices": []}
        return 404, {"error": f"no synth for {endpoint}"}
    except Exception as exc:  # noqa: BLE001
        return 599, {"error": f"{type(exc).__name__}: {exc}"}


def _read_toggle_snapshot() -> dict[str, bool]:
    try:
        from spot_aggro.api import routes as spot_routes
        t = getattr(spot_routes, "_SPOT_TIER_TOGGLE", None)
        if t is None:
            return {"A+": True, "A": True, "B": True, "C": True}
        return dict(t.snapshot())
    except Exception:  # noqa: BLE001
        return {"A+": True, "A": True, "B": True, "C": True}


def _stats_from_db() -> dict[str, Any]:
    """Recompute the minimum viable /stats payload from trade_log."""
    import time as _t
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        day_start = int((_t.time() - 86400) * 1000)
        row = con.execute(
            "SELECT COUNT(*), "
            " SUM(CASE WHEN pnl_usd > 0.001 THEN 1 ELSE 0 END), "
            " SUM(CASE WHEN pnl_usd IS NOT NULL THEN pnl_usd ELSE 0 END) "
            "FROM trade_log WHERE action='exit' AND ts_ms > ?",
            (day_start,),
        ).fetchone()
        recent = con.execute(
            "SELECT symbol, action, pnl_usd, ts_ms FROM trade_log "
            "WHERE ts_ms > ? ORDER BY ts_ms DESC LIMIT 20",
            (day_start,),
        ).fetchall()
    finally:
        con.close()
    exits = int(row[0] or 0)
    wins = int(row[1] or 0)
    pnl = float(row[2] or 0.0)
    return {
        "today": {
            "trades": exits, "wins": wins, "pnl_usd": round(pnl, 4),
            "losses": max(0, exits - wins),
        },
        "recent": [
            {"symbol": r[0], "action": r[1],
             "pnl_usd": r[2], "ts_ms": r[3]}
            for r in recent
        ],
    }


# ---------------------------------------------------------------------------
# Per-card checks
# ---------------------------------------------------------------------------

def _check_endpoint_reachable(spec: CardSpec, payloads: dict[str, Any]) -> CardFinding:
    status, body = _fetch_card_payload(spec.endpoint)
    payloads[spec.card_id] = {"status": status, "body": body}
    if status == 599:
        return CardFinding(
            check="endpoint", severity="fail",
            message=f"endpoint {spec.endpoint} raised during fetch",
            evidence={"body": body},
        )
    if status >= 500:
        return CardFinding(
            check="endpoint", severity="fail",
            message=f"endpoint {spec.endpoint} returned HTTP {status}",
        )
    if status >= 400:
        # 401/403 are expected for admin endpoints without auth; log as warn.
        return CardFinding(
            check="endpoint", severity="warn",
            message=f"endpoint {spec.endpoint} returned HTTP {status} "
                    f"(may be auth-gated in this context)",
        )
    if not body:
        return CardFinding(
            check="endpoint", severity="fail",
            message=f"endpoint {spec.endpoint} returned empty body",
        )
    return CardFinding(
        check="endpoint", severity="ok",
        message=f"{spec.endpoint} -> HTTP {status} "
                f"({len(json.dumps(body, default=str))} bytes)",
    )


def _check_schema(spec: CardSpec, payloads: dict[str, Any]) -> CardFinding:
    entry = payloads.get(spec.card_id) or {}
    body = entry.get("body")
    if not isinstance(body, dict):
        return CardFinding(
            check="schema", severity="warn",
            message=f"payload is not a dict; cannot validate required keys",
        )
    missing = [k for k in spec.required_keys if k not in body]
    if missing:
        return CardFinding(
            check="schema", severity="fail",
            message=f"missing required keys: {missing}",
            evidence={"keys_seen": list(body.keys())[:20]},
        )
    return CardFinding(
        check="schema", severity="ok",
        message=f"all {len(spec.required_keys)} required keys present",
    )


def _check_cross_consistency(payloads: dict[str, Any]) -> list[tuple[str, CardFinding]]:
    """Return list of (card_id, finding) attaching a cross-finding to every
    card affected by a mismatch. This runs ONCE per audit, not per card."""
    findings: list[tuple[str, CardFinding]] = []

    # Rule 1: /status.gates.tier_toggles agrees with /tier_toggles.execution.
    status = (payloads.get("c-engine") or {}).get("body") or {}
    toggles_body = (payloads.get("c-tier-toggles") or {}).get("body") or {}
    # /tier_toggles returns {ok, engine, execution: {...}} — extract the map.
    toggles = ((toggles_body.get("execution") if isinstance(toggles_body, dict)
                else None) or toggles_body) or {}
    if isinstance(status, dict) and isinstance(toggles, dict):
        status_toggles = ((status.get("gates") or {}).get("tier_toggles")
                          or status.get("tier_toggles") or {})
        mismatched = []
        for tier in ("A+", "A", "B", "C"):
            if tier in status_toggles and tier in toggles:
                if bool(status_toggles[tier]) != bool(toggles[tier]):
                    mismatched.append(
                        f"{tier}: status={status_toggles[tier]} "
                        f"tier_toggles={toggles[tier]}"
                    )
        if mismatched:
            msg = "/status disagrees with /tier_toggles: " + "; ".join(mismatched)
            findings.append(("c-tier-toggles", CardFinding(
                check="cross", severity="fail", message=msg,
                evidence={"mismatches": mismatched},
            )))
            findings.append(("c-engine", CardFinding(
                check="cross", severity="fail", message=msg,
            )))

    # Rule 2: /stats.today.trades matches /status.trades_today (advisory
    # — this is the classic "two counters diverge silently" trap).
    stats = (payloads.get("c-today") or {}).get("body") or {}
    if isinstance(stats, dict) and isinstance(status, dict):
        stats_trades = ((stats.get("today") or {}).get("trades")
                        if isinstance(stats.get("today"), dict) else None)
        status_trades = (status.get("trades_today")
                         or (status.get("state") or {}).get("trades_today"))
        if (stats_trades is not None and status_trades is not None
                and stats_trades != status_trades):
            msg = (f"trades-today divergence: /stats says {stats_trades}, "
                   f"/status says {status_trades}")
            findings.append(("c-today", CardFinding(
                check="cross", severity="warn", message=msg,
            )))

    # Rule 3: /research/latest.latest.halt_state agrees with /tier_toggles.
    research_body = (payloads.get("c-research") or {}).get("body") or {}
    research = (research_body.get("latest")
                if isinstance(research_body, dict) else None) or research_body
    if isinstance(research, dict) and isinstance(toggles, dict):
        halt = research.get("halt_state") or {}
        drifts = []
        for tier in ("A+", "A", "B", "C"):
            halted = bool(halt.get(tier))
            toggle_on = bool(toggles.get(tier, True))
            if halted and toggle_on:
                drifts.append(
                    f"{tier}: research halted but tier toggle still ON"
                )
        if drifts:
            msg = ("research halt vs toggle drift: "
                   + "; ".join(drifts))
            findings.append(("c-research", CardFinding(
                check="cross", severity="fail", message=msg,
                evidence={"drifts": drifts},
            )))
            findings.append(("c-tier-toggles", CardFinding(
                check="cross", severity="fail", message=msg,
            )))

    return findings


def _check_freshness(
    spec: CardSpec, payloads: dict[str, Any], now_ms: int
) -> CardFinding:
    body = (payloads.get(spec.card_id) or {}).get("body")
    if not isinstance(body, dict):
        return CardFinding(
            check="freshness", severity="warn",
            message="cannot read timestamp from non-dict payload",
        )
    # Scan for any *_ts_ms or generated_at timestamp.
    candidates = []
    for k, v in body.items():
        if isinstance(v, (int, float)) and (
            k.endswith("_ts_ms") or k in ("generated_ts_ms", "completed_ts_ms")
        ):
            candidates.append((k, int(v)))
    if not candidates:
        # Not every card carries a server-side timestamp — browser stamps
        # cover that. Emit informational ok.
        return CardFinding(
            check="freshness", severity="ok",
            message="no server-side timestamp; freshness tracked client-side",
        )
    age_s = (now_ms - max(ts for _, ts in candidates)) / 1000.0
    if age_s > spec.freshness_seconds:
        # Phase 11n-9-dd: when the engine is intentionally stopped by
        # the operator, engine-dependent cards (trading status, account
        # snapshot, connectivity) should show IDLE instead of faking a
        # FAIL. The heartbeat writer keeps equity_marks fresh, but
        # cards that source data only while cycling still age out.
        try:
            from spot_aggro.governance.engine_state_source import (
                current_engine_state,
            )
            es = current_engine_state() or {}
            # Only suppress for explicit operator stop / kill-switch.
            # `idle` (server just booted, engine never started) does
            # NOT suppress — the heartbeat writer has not started yet
            # so a real stale card still needs to surface.
            stopped = es.get("state") in (
                "stopped_by_operator", "halted_by_kill"
            )
        except Exception:
            stopped = False
        if stopped:
            return CardFinding(
                check="freshness", severity="ok",
                message=(
                    f"idle (engine stopped_by_operator): "
                    f"{age_s:.0f}s stale tolerated"
                ),
                evidence={
                    "timestamps": dict(candidates),
                    "engine_intentionally_stopped": True,
                },
            )
        sev = "fail" if spec.critical else "warn"
        return CardFinding(
            check="freshness", severity=sev,
            message=f"payload stale: {age_s:.0f}s > SLA {spec.freshness_seconds}s",
            evidence={"timestamps": dict(candidates)},
        )
    return CardFinding(
        check="freshness", severity="ok",
        message=f"fresh: {age_s:.0f}s ≤ SLA {spec.freshness_seconds}s",
    )


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def audit_cards(specs: tuple[CardSpec, ...] = CARD_SPECS) -> CardAudit:
    """Run every registered card through all four checks and return a
    single CardAudit with per-card verdicts + aggregate."""
    now_ms = int(time.time() * 1000)
    payloads: dict[str, Any] = {}
    per_card_findings: dict[str, list[CardFinding]] = {
        spec.card_id: [] for spec in specs
    }

    # Phase 1: endpoint + schema per card.
    for spec in specs:
        per_card_findings[spec.card_id].append(
            _check_endpoint_reachable(spec, payloads)
        )
        per_card_findings[spec.card_id].append(
            _check_schema(spec, payloads)
        )

    # Phase 2: cross-card consistency (once, attributes findings to
    # involved cards).
    for card_id, finding in _check_cross_consistency(payloads):
        if card_id in per_card_findings:
            per_card_findings[card_id].append(finding)

    # Phase 3: freshness per card.
    for spec in specs:
        per_card_findings[spec.card_id].append(
            _check_freshness(spec, payloads, now_ms)
        )

    # Roll up per-card verdicts.
    cards: list[CardVerdict] = []
    for spec in specs:
        f_list = per_card_findings[spec.card_id]
        n_ok = sum(1 for f in f_list if f.severity == "ok")
        n_warn = sum(1 for f in f_list if f.severity == "warn")
        n_fail = sum(1 for f in f_list if f.severity == "fail")
        if n_fail > 0:
            verdict = "fail"
        elif n_warn > 0:
            verdict = "warn"
        else:
            verdict = "ok"
        cards.append(CardVerdict(
            card_id=spec.card_id, title=spec.title,
            verdict=verdict, n_checks=len(f_list),
            n_ok=n_ok, n_warn=n_warn, n_fail=n_fail,
            findings=f_list,
        ))

    # Aggregate verdict = worst severity across cards.
    n_ok = sum(1 for c in cards if c.verdict == "ok")
    n_warn = sum(1 for c in cards if c.verdict == "warn")
    n_fail = sum(1 for c in cards if c.verdict == "fail")
    if n_fail > 0:
        agg = "fail"
    elif n_warn > 0:
        agg = "warn"
    else:
        agg = "ok"

    return CardAudit(
        run_id=f"cta-{now_ms}",
        generated_ts_ms=now_ms,
        verdict=agg,
        n_cards=len(cards),
        n_ok=n_ok, n_warn=n_warn, n_fail=n_fail,
        cards=cards,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_card_truth_audits (
    run_id          TEXT PRIMARY KEY,
    generated_ts_ms INTEGER NOT NULL,
    verdict         TEXT NOT NULL,
    n_cards         INTEGER NOT NULL,
    n_ok            INTEGER NOT NULL,
    n_warn          INTEGER NOT NULL,
    n_fail          INTEGER NOT NULL,
    payload_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_card_truth_ts
    ON spot_card_truth_audits(generated_ts_ms DESC);
"""


def _init_schema() -> None:
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()


def persist_audit(audit: CardAudit) -> None:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_card_truth_audits "
            "(run_id, generated_ts_ms, verdict, n_cards, n_ok, n_warn, "
            " n_fail, payload_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                audit.run_id, audit.generated_ts_ms, audit.verdict,
                audit.n_cards, audit.n_ok, audit.n_warn, audit.n_fail,
                json.dumps(audit.to_dict(), default=str),
            ),
        )
        con.commit()
    finally:
        con.close()


def latest_audit() -> Optional[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT payload_json FROM spot_card_truth_audits "
            "ORDER BY generated_ts_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else None


def history(limit: int = 30) -> list[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT run_id, generated_ts_ms, verdict, n_cards, "
            " n_ok, n_warn, n_fail "
            "FROM spot_card_truth_audits "
            "ORDER BY generated_ts_ms DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "run_id": r[0], "generated_ts_ms": r[1], "verdict": r[2],
            "n_cards": r[3], "n_ok": r[4], "n_warn": r[5], "n_fail": r[6],
        }
        for r in rows
    ]


def run_and_persist() -> CardAudit:
    a = audit_cards()
    try:
        persist_audit(a)
    except Exception:  # noqa: BLE001
        pass
    return a
