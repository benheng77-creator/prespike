"""
Key audit tool — for every secret in .env, report:

    SET?          is the key present and non-empty
    INTEGRATED?   does code in this repo actually read it
    LIVE TEST     can we authenticate / reach the service with it

Secret values are never printed. Only lengths, prefixes, and pass/fail.

Run it:
    cd openclaw_v1
    python scripts/test_keys.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # openclaw_v1/
REPO = ROOT.parent                      # trading-bot/

# Load .env without needing python-dotenv (minimal parser).
def _load_env() -> dict[str, str]:
    envs = {}
    candidates = [
        os.environ.get("OPENCLAW_ENV_FILE"),
        str(REPO / ".env"),
        str(ROOT / "config" / ".env"),
        str(ROOT / ".env"),
    ]
    for c in candidates:
        if not c or not Path(c).exists():
            continue
        for raw in Path(c).read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            # First occurrence wins (matches OPENCLAW_ENV_FILE precedence order)
            envs.setdefault(k, v)
    return envs


ENV = _load_env()


def _http_get(url: str, headers: Optional[dict] = None, timeout: float = 5.0) -> tuple[int, str]:
    """Return (status_code, body[:500]) — short timeout. 0 / '' on network error."""
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(2048).decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        try:
            body = e.read(2048).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return 0, str(e)


def _http_post(url: str, payload: dict, headers: Optional[dict] = None,
               timeout: float = 8.0) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    h = {"content-type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(2048).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(2048).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return 0, str(e)


# ---------------------------------------------------------------------------
# Probes — one per key or per service
# ---------------------------------------------------------------------------

def _probe_openai(v: str) -> tuple[str, str]:
    code, _ = _http_get("https://api.openai.com/v1/models",
                       headers={"Authorization": f"Bearer {v}"})
    if code == 200: return "ok", "authed · listed models"
    if code == 401: return "fail", "invalid key (401)"
    if code == 0:   return "warn", "network unreachable"
    return "warn", f"unexpected http {code}"


def _probe_anthropic(v: str) -> tuple[str, str]:
    code, _ = _http_get(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": v, "anthropic-version": "2023-06-01"},
    )
    if code == 200: return "ok", "authed · listed models"
    if code in (401, 403): return "fail", f"invalid key ({code})"
    if code == 0:   return "warn", "network unreachable"
    return "warn", f"unexpected http {code}"


def _probe_gemini(v: str) -> tuple[str, str]:
    code, _ = _http_get(f"https://generativelanguage.googleapis.com/v1beta/models?key={v}")
    if code == 200: return "ok", "authed · listed models"
    if code in (400, 401, 403): return "fail", f"invalid key ({code})"
    if code == 0:   return "warn", "network unreachable"
    return "warn", f"unexpected http {code}"


def _probe_coingecko(v: str) -> tuple[str, str]:
    # Pro header first, fall back to demo
    code, _ = _http_get("https://pro-api.coingecko.com/api/v3/ping",
                       headers={"x-cg-pro-api-key": v})
    if code == 200: return "ok", "pro authed"
    code2, _ = _http_get(f"https://api.coingecko.com/api/v3/ping?x_cg_demo_api_key={v}")
    if code2 == 200: return "ok", "demo authed"
    if 0 in (code, code2): return "warn", "network unreachable"
    return "fail", f"pro:{code} demo:{code2}"


def _probe_cmc(v: str) -> tuple[str, str]:
    code, _ = _http_get("https://pro-api.coinmarketcap.com/v1/key/info",
                       headers={"X-CMC_PRO_API_KEY": v})
    if code == 200: return "ok", "authed"
    if code in (401, 403): return "fail", f"invalid ({code})"
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_cryptocompare(v: str) -> tuple[str, str]:
    code, body = _http_get(
        "https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD",
        headers={"authorization": f"Apikey {v}"},
    )
    if code == 200 and "USD" in body: return "ok", "priced BTC"
    if code in (401, 403): return "fail", f"invalid ({code})"
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_newsapi(v: str) -> tuple[str, str]:
    code, _ = _http_get(f"https://newsapi.org/v2/top-headlines?country=us&pageSize=1&apiKey={v}")
    if code == 200: return "ok", "fetched headlines"
    if code in (401, 403, 426, 429): return "fail", f"http {code}"
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_telegram(v: str) -> tuple[str, str]:
    code, body = _http_get(f"https://api.telegram.org/bot{v}/getMe")
    if code == 200 and '"ok":true' in body: return "ok", "bot reachable"
    if code in (401, 404): return "fail", f"bad token ({code})"
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_telegram_chat(chat_id: str) -> tuple[str, str]:
    token = ENV.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return "skip", "requires TELEGRAM_BOT_TOKEN"
    if os.environ.get("CLAW_AUDIT_SKIP_SEND") == "1":
        return "skip", "skipped (CLAW_AUDIT_SKIP_SEND=1)"
    code, body = _http_post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": chat_id.strip(), "text": "claw247 key audit ping · delete me"},
    )
    if code == 200 and '"ok":true' in body: return "ok", "test message delivered"
    if code in (400, 401, 403):
        reason = "chat not found — message the bot once first" if code == 400 else f"auth ({code})"
        return "fail", reason
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_mistral(v: str) -> tuple[str, str]:
    code, _ = _http_get(
        "https://api.mistral.ai/v1/models",
        headers={"Authorization": f"Bearer {v}"},
    )
    if code == 200: return "ok", "authed · listed models"
    if code in (401, 403): return "fail", f"invalid key ({code})"
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_openrouter(v: str) -> tuple[str, str]:
    code, _ = _http_get(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {v}"},
    )
    if code == 200: return "ok", "authed · key ok"
    if code in (401, 403): return "fail", f"invalid key ({code})"
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_callmebot(api_key: str) -> tuple[str, str]:
    phone = ENV.get("CALLMEBOT_PHONE", "").strip()
    if not phone:
        return "fail", "CALLMEBOT_PHONE not set"
    if not phone.startswith("+") or not phone[1:].isdigit():
        return "fail", "CALLMEBOT_PHONE must look like +CountryCodeNumber"
    if os.environ.get("CLAW_AUDIT_SKIP_SEND") == "1":
        return "skip", "skipped (CLAW_AUDIT_SKIP_SEND=1)"
    import urllib.parse as _up
    msg = _up.quote("claw247 key audit ping · delete me")
    url = (
        "https://api.callmebot.com/whatsapp.php"
        f"?phone={_up.quote(phone)}&text={msg}&apikey={_up.quote(api_key)}"
    )
    code, body = _http_get(url, timeout=10.0)
    if code == 200 and ("sent" in body.lower() or "message queued" in body.lower()):
        return "ok", "whatsapp test delivered"
    if code == 200 and "apikey" in body.lower():
        return "fail", "api key not accepted — re-pair with CallMeBot bot"
    if code == 0: return "warn", "network unreachable"
    # CallMeBot returns 200 + HTML on errors too; heuristic
    if "not registered" in body.lower() or "not allowed" in body.lower():
        return "fail", "phone not paired with CallMeBot yet"
    return "warn", f"http {code} · check phone/key pairing"


def _probe_x_bearer(v: str) -> tuple[str, str]:
    code, _ = _http_get("https://api.twitter.com/2/tweets/counts/recent?query=bitcoin",
                       headers={"Authorization": f"Bearer {v}"})
    if code == 200: return "ok", "v2 query ok"
    if code in (401, 403): return "fail", f"invalid ({code})"
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_helius(v: str) -> tuple[str, str]:
    # Helius: JSON-RPC with API key in URL param
    code, _ = _http_post(
        f"https://mainnet.helius-rpc.com/?api-key={v}",
        {"jsonrpc": "2.0", "id": 1, "method": "getSlot"},
    )
    if code == 200: return "ok", "rpc responded"
    if code in (401, 403): return "fail", f"invalid ({code})"
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_alchemy(v: str) -> tuple[str, str]:
    # Alchemy: JSON-RPC on eth-mainnet
    code, _ = _http_post(
        f"https://eth-mainnet.g.alchemy.com/v2/{v}",
        {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
    )
    if code == 200: return "ok", "eth rpc responded"
    if code in (401, 403): return "fail", f"invalid ({code})"
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_dune(v: str) -> tuple[str, str]:
    # Minimal auth check: /api/v1/execution/{id}/results — we just probe /health
    code, _ = _http_get("https://api.dune.com/api/v1/health",
                       headers={"X-Dune-API-Key": v})
    if code == 200: return "ok", "health ok"
    if code in (401, 403): return "fail", f"invalid ({code})"
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_nansen(v: str) -> tuple[str, str]:
    code, _ = _http_get("https://api.nansen.ai/api/v1/health",
                       headers={"apiKey": v})
    if code == 200: return "ok", "health ok"
    if code in (401, 403): return "fail", f"invalid ({code})"
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_arkham(v: str) -> tuple[str, str]:
    # Arkham private API — best-effort
    code, _ = _http_get("https://api.arkm.com/intelligence/ping",
                       headers={"X-Payload-Api-Key": v})
    if code == 200: return "ok", "ping ok"
    if code in (401, 403): return "fail", f"invalid ({code})"
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_glassnode(v: str) -> tuple[str, str]:
    code, _ = _http_get(
        f"https://api.glassnode.com/v1/metrics/market/price_usd_close?a=BTC&s={int(time.time())-3600}&api_key={v}"
    )
    if code == 200: return "ok", "auth accepted"
    if code in (401, 403): return "fail", f"invalid ({code})"
    if code == 0: return "warn", "network unreachable"
    return "warn", f"http {code}"


def _probe_exchange_ccxt(exchange_id: str) -> tuple[str, str]:
    try:
        import ccxt  # type: ignore
    except ImportError:
        return "skip", "ccxt not installed (pip install ccxt)"
    if not hasattr(ccxt, exchange_id.lower()):
        return "fail", f"ccxt has no class {exchange_id!r}"
    klass = getattr(ccxt, exchange_id.lower())
    try:
        cfg = {}
        if exchange_id.lower() == "binance":
            cfg = {"apiKey": ENV.get("API_KEY", ""), "secret": ENV.get("API_SECRET", "")}
        elif exchange_id.lower() == "coinbase":
            cfg = {"apiKey": ENV.get("COINBASE_API_KEY", ""),
                   "secret": ENV.get("COINBASE_API_SECRET", ""),
                   "password": ENV.get("COINBASE_PASSPHRASE", "") or None}
        elif exchange_id.lower() == "okx":
            cfg = {"apiKey": ENV.get("OKX_API_KEY", ""),
                   "secret": ENV.get("OKX_API_SECRET", ""),
                   "password": ENV.get("OKX_PASSPHRASE", "")}
        elif exchange_id.lower() == "cryptocom":
            cfg = {"apiKey": ENV.get("CRYPTOCOM_API_KEY", ""),
                   "secret": ENV.get("CRYPTOCOM_API_SECRET", "")}
        elif exchange_id.lower() == "independentreserve":
            cfg = {"apiKey": ENV.get("INDEPENDENTRESERVE_API_KEY", ""),
                   "secret": ENV.get("INDEPENDENTRESERVE_API_SECRET", "")}
        ex = klass({k: v for k, v in cfg.items() if v})
        try:
            ex.load_markets()
        except Exception as e:
            return "warn", f"load_markets failed: {type(e).__name__}"
        # Only hit private endpoint if key is present — otherwise leave public-only
        if not cfg.get("apiKey"):
            return "ok", "public reachable (no private key to test)"
        try:
            ex.fetch_balance()
            return "ok", "private authed · balance ok"
        except Exception as e:
            return "fail", f"auth failed: {type(e).__name__}"
    except Exception as e:
        return "warn", f"init error: {type(e).__name__}: {e}"


@dataclass
class Check:
    name: str
    category: str
    probe: Optional[Callable[[str], tuple[str, str]]] = None
    no_probe_reason: str = ""


CHECKS: list[Check] = [
    # Admin / modes (flag-only)
    Check("OPS_ADMIN_TOKEN", "Admin", no_probe_reason="format only — used by /ops/*"),
    Check("CLAW_MODE",       "Admin", no_probe_reason="value echoed in /status"),
    Check("CLAW_VERSION",    "Admin", no_probe_reason="label"),

    # LLM
    Check("OPENAI_API_KEY",    "LLM", _probe_openai),
    Check("ANTHROPIC_API_KEY", "LLM", _probe_anthropic),
    Check("CLAUDE_API_KEY",    "LLM", _probe_anthropic),     # alias
    Check("GEMINI_API_KEY",    "LLM", _probe_gemini),
    Check("CODEX_API_KEY",     "LLM", _probe_openai),        # OpenAI fallback

    # Market data
    Check("COINGECKO_API_KEY",     "MarketData", _probe_coingecko),
    Check("CRYPTOCOMPARE_API_KEY", "MarketData", _probe_cryptocompare),
    Check("CMC_API_KEY",           "MarketData", _probe_cmc),
    Check("GLASSNODE_API_KEY",     "MarketData", _probe_glassnode),
    Check("NANSEN_API_KEY",        "MarketData", _probe_nansen),
    Check("DUNE_API_KEY",          "MarketData", _probe_dune),
    Check("ARKHAM_API_KEY",        "MarketData", _probe_arkham),
    Check("HELIUS_API_KEY",        "MarketData", _probe_helius),
    Check("ALCHEMY_API_KEY",       "MarketData", _probe_alchemy),

    # News / social
    Check("NEWS_API_KEY",       "News", _probe_newsapi),
    Check("X_BEARER_TOKEN",     "News", _probe_x_bearer),

    # Notify
    Check("TELEGRAM_BOT_TOKEN", "Notify", _probe_telegram),
    Check("TELEGRAM_CHAT_ID",   "Notify", _probe_telegram_chat),
    Check("CALLMEBOT_API_KEY",  "Notify", _probe_callmebot),
    Check("CALLMEBOT_PHONE",    "Notify", no_probe_reason="format check (needed by CALLMEBOT probe)"),

    # Extra LLM providers
    Check("MISTRAL_API_KEY",    "LLM", _probe_mistral),
    Check("OPENROUTER_API_KEY", "LLM", _probe_openrouter),
]


# Which env var is actually *referenced* by code in this repo?
def _integrated_keys() -> set[str]:
    want = {c.name for c in CHECKS}
    # Add exchange-pair keys + misc.
    want |= {
        "API_KEY", "API_SECRET",
        "COINBASE_API_KEY", "COINBASE_API_SECRET", "COINBASE_PASSPHRASE",
        "OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE",
        "CRYPTOCOM_API_KEY", "CRYPTOCOM_API_SECRET",
        "INDEPENDENTRESERVE_API_KEY", "INDEPENDENTRESERVE_API_SECRET",
        "ACTIVE_EXCHANGE", "EXCHANGE_ID", "EXCHANGE_PASSPHRASE",
        "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT",
        "TELEGRAM_CHAT_ID",
        "CALLMEBOT_API_KEY", "CALLMEBOT_PHONE",
        "MISTRAL_API_KEY", "OPENROUTER_API_KEY",
        "ENCRYPTION_KEY", "TRADE_DB_PATH", "OPENCLAW_AUDIT_JSONL",
        "OPENCLAW_LIVE_TRADING", "OPENCLAW_ORCHESTRATOR", "OPENCLAW_DAYTRADE",
        "BINARY15M_SIGNING_SECRET", "BINARY15_SIGNING_SECRET",
        "OPENCLAW_BINARY15M", "OPENCLAW_BINARY15",
        # Phase 11n-9-t — OPENCLAW_99X_APEX, APEX_V2_ENABLED,
        # ACTIVE_STRATEGY removed (deep-apex purge).
    }
    seen: set[str] = set()
    # Exclude the audit script itself, tests, and scripts/ — only production
    # code counts as "wired in".
    skip_parts = ("scripts", "tests", "__pycache__", "runtime")
    for p in ROOT.rglob("*.py"):
        if any(part in p.parts for part in skip_parts):
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for name in want:
            if name in seen:
                continue
            if name in txt:
                seen.add(name)
    return seen


def _mask(v: str) -> str:
    if not v: return "(empty)"
    if len(v) <= 8: return f"len={len(v)}"
    return f"{v[:3]}…{v[-3:]} len={len(v)}"


def main() -> int:
    print()
    print("=" * 72)
    print(" claw247 KEY AUDIT — secrets are never printed in full")
    print("=" * 72)
    print(f" .env discovered: {sum(1 for k in ENV)} keys loaded")
    print()

    integrated = _integrated_keys()

    col_fmt = "{:<30} {:<12} {:<8} {:<8} {:<30}"
    print(col_fmt.format("KEY", "CATEGORY", "SET", "WIRED", "LIVE TEST"))
    print("-" * 100)

    # Exchange flow — special handling
    active_ex = ENV.get("ACTIVE_EXCHANGE", "").strip().lower()
    if active_ex:
        status, msg = _probe_exchange_ccxt(active_ex)
        print(col_fmt.format(
            f"ACTIVE_EXCHANGE={active_ex}", "Exchange", "YES",
            "YES" if "ACTIVE_EXCHANGE" in integrated else "no",
            f"[{status}] {msg}",
        ))

    for chk in CHECKS:
        val = ENV.get(chk.name, "").strip()
        is_set = "YES" if val else "no"
        wired = "YES" if chk.name in integrated else "no"
        if not val:
            live = "— (not set)"
        elif chk.probe is None:
            live = f"[info] {chk.no_probe_reason}"
        else:
            try:
                status, msg = chk.probe(val)
            except Exception as e:
                status, msg = "warn", f"probe error: {type(e).__name__}"
            live = f"[{status}] {msg}"
        print(col_fmt.format(chk.name, chk.category, is_set, wired, live))

    print()
    print("Legend:")
    print("  [ok]    key authenticates and service responded")
    print("  [fail]  service rejected the key (bad / expired / wrong scope)")
    print("  [warn]  network or unexpected response — retry or check firewall")
    print("  [skip]  library missing, install to enable (e.g. pip install ccxt)")
    print("  [info]  config flag / admin token — no remote test")
    print("  YES in WIRED = something in this repo reads the env var")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
