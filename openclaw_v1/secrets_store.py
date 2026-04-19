"""
Secrets store for the trader host.

Authoritative location: a single `.env` file (default `.env` in cwd, override
via `OPENCLAW_ENV_FILE`). Browser never sees real values — the server
returns a *masked* view, and mutations arrive through an admin-gated
POST/DELETE.

Design rules:
- Read / write the `.env` file directly (python-dotenv-compatible format).
- Preserve ordering, comments, and blank lines during writes.
- Mask values on read: show first 4 + last 4 chars, replace middle with "•".
- Keep a short in-process cache for group/category metadata.
- Every mutation writes an audit row via AuditLedger.

The store does NOT reload os.environ for running processes. The trader
re-reads `.env` on restart, and `live_executor` treats env as the runtime
source of truth — so rotating a key requires a trader restart (documented
in the UI).
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# ---- key catalog ----------------------------------------------------------
# Grouped for UI; "sensitive" flag controls whether the value is masked.

@dataclass
class KeyDef:
    name: str
    group: str
    label: str
    purpose: str
    sensitive: bool = True
    required: bool = False


CATALOG: list[KeyDef] = [
    # Exchange — generic (ccxt fallback)
    KeyDef("API_KEY",          "Exchange", "CCXT API key",            "Generic ccxt exchange API key (Binance/etc)", True),
    KeyDef("API_SECRET",       "Exchange", "CCXT API secret",         "Generic ccxt exchange API secret", True),

    # Exchange — Coinbase Advanced Trade
    KeyDef("COINBASE_API_KEY",     "Exchange", "Coinbase API key",       "Coinbase Advanced Trade key", True),
    KeyDef("COINBASE_API_SECRET",  "Exchange", "Coinbase API secret",    "Coinbase Advanced Trade secret", True),
    KeyDef("COINBASE_PASSPHRASE",  "Exchange", "Coinbase passphrase",    "Required by legacy Pro API only", True),

    # Exchange — OKX
    KeyDef("OKX_API_KEY",          "Exchange", "OKX API key",            "OKX REST/WebSocket key", True),
    KeyDef("OKX_API_SECRET",       "Exchange", "OKX API secret",         "OKX secret", True),
    KeyDef("OKX_PASSPHRASE",       "Exchange", "OKX passphrase",         "Set when you created the key", True),

    # Exchange — Crypto.com
    KeyDef("CRYPTOCOM_API_KEY",    "Exchange", "Crypto.com API key",     "Crypto.com Exchange key", True),
    KeyDef("CRYPTOCOM_API_SECRET", "Exchange", "Crypto.com API secret",  "Crypto.com Exchange secret", True),

    # Exchange — Independent Reserve
    KeyDef("INDEPENDENTRESERVE_API_KEY",    "Exchange", "IndependentReserve API key",    "IR key", True),
    KeyDef("INDEPENDENTRESERVE_API_SECRET", "Exchange", "IndependentReserve API secret", "IR secret", True),

    # Exchange — Moomoo
    KeyDef("MOOMOO_HOST",      "Exchange", "Moomoo host",             "OpenD host", False),
    KeyDef("MOOMOO_PORT",      "Exchange", "Moomoo port",             "OpenD port", False),
    KeyDef("MOOMOO_MARKET",    "Exchange", "Moomoo market",           "HK / US / CN", False),
    KeyDef("MOOMOO_TRADE_ENV", "Exchange", "Moomoo trade env",        "SIMULATE / REAL", False),
    KeyDef("MOOMOO_ACCOUNT_ID","Exchange", "Moomoo account id",       "", False),
    KeyDef("MOOMOO_TRADE_PASSWORD", "Exchange", "Moomoo trade pwd",   "Plain pwd (will be md5'd)", True),
    KeyDef("MOOMOO_TRADE_PWD_MD5",  "Exchange", "Moomoo trade pwd md5","Pre-hashed", True),
    KeyDef("MOOMOO_RSA_PRIVATE_KEY","Exchange", "Moomoo RSA priv key","RSA private key text", True),

    # Active exchange selector
    KeyDef("ACTIVE_EXCHANGE",  "Exchange", "Active exchange id",      "ccxt id: binance | coinbase | okx | cryptocom | independentreserve | moomoo", False),

    # LLM / sentiment — API keys
    KeyDef("OPENAI_API_KEY",       "LLM",      "OpenAI",              "Sentiment / LLM features", True),
    KeyDef("ANTHROPIC_API_KEY",    "LLM",      "Anthropic",           "Sentiment / LLM features", True),
    KeyDef("CLAUDE_API_KEY",       "LLM",      "Claude (alias)",      "Alias for Anthropic; used by Claude Code / SDK integrations", True),
    KeyDef("GEMINI_API_KEY",       "LLM",      "Gemini",              "Google Gemini API / CLI", True),
    KeyDef("CODEX_API_KEY",        "LLM",      "Codex",               "OpenAI Codex / Codex CLI — falls back to OPENAI_API_KEY", True),

    # Market data
    KeyDef("COINGECKO_API_KEY",    "MarketData", "CoinGecko",         "Price + market data", True),
    KeyDef("CRYPTOCOMPARE_API_KEY","MarketData", "CryptoCompare",     "Price + market data", True),
    KeyDef("CMC_API_KEY",          "MarketData", "CoinMarketCap",     "Price + market data", True),
    KeyDef("GLASSNODE_API_KEY",    "MarketData", "Glassnode",         "On-chain metrics", True),
    KeyDef("NANSEN_API_KEY",       "MarketData", "Nansen",            "On-chain + wallets", True),
    KeyDef("DUNE_API_KEY",         "MarketData", "Dune",              "SQL queries", True),
    KeyDef("ARKHAM_API_KEY",       "MarketData", "Arkham",            "Entity / on-chain", True),
    KeyDef("HELIUS_API_KEY",       "MarketData", "Helius",            "Solana RPC / enrich", True),
    KeyDef("ALCHEMY_API_KEY",      "MarketData", "Alchemy",           "EVM RPC", True),

    # News / social
    KeyDef("NEWS_API_KEY",         "News",     "NewsAPI",             "News feed for sentiment", True),
    KeyDef("X_BEARER_TOKEN",       "News",     "X (Twitter) bearer",  "Social sentiment", True),
    KeyDef("REDDIT_CLIENT_ID",     "News",     "Reddit client id",    "Social sentiment", True),
    KeyDef("REDDIT_CLIENT_SECRET", "News",     "Reddit client secret","Social sentiment", True),
    KeyDef("REDDIT_USER_AGENT",    "News",     "Reddit user agent",   "Identify bot", False),

    # Notifications
    KeyDef("TELEGRAM_BOT_TOKEN",   "Notify",   "Telegram bot token",  "EOD + alert push", True),
    KeyDef("TELEGRAM_CHAT_ID",     "Notify",   "Telegram chat id",    "Destination chat", False),

    # Infrastructure
    KeyDef("ENCRYPTION_KEY",       "Infra",    "Encryption key",      "Secret-at-rest crypto", True),
    KeyDef("TRADE_DB_PATH",        "Infra",    "Trade DB path",       "Default trades.db", False),
    KeyDef("OPENCLAW_AUDIT_JSONL", "Infra",    "Audit JSONL path",    "Default logs/openclaw.jsonl", False),

    # Modes / feature flags
    KeyDef("CLAW_MODE",                "Modes", "Mode",                "paper / live", False),
    KeyDef("CLAW_VERSION",             "Modes", "Version label",       "", False),
    KeyDef("CLAW_CORS_ORIGINS",        "Modes", "CORS origins",        "* for internal", False),
    KeyDef("CLAW_GATE_THRESHOLD",      "Modes", "Accuracy gate thr",   "0.75 default", False),
    KeyDef("OPENCLAW_LIVE_TRADING",    "Modes", "Live trading flag",   "1 = enable live orders", False),
    KeyDef("OPENCLAW_ORCHESTRATOR",    "Modes", "Orchestrator flag",   "1 = attach supervisor", False),
    KeyDef("OPENCLAW_DAYTRADE",        "Modes", "Daytrade flag",       "1 = enable daytrade runner", False),

    # Ops
    KeyDef("OPS_ADMIN_TOKEN",          "Ops",   "Admin token",         "Required for secret edits", True),

    # binary15m (directional)
    KeyDef("BINARY15M_SIGNING_SECRET", "Binary15m", "Signing secret",  "HMAC-SHA256 secret; required to emit signed payloads", True),
    KeyDef("OPENCLAW_BINARY15M",       "Binary15m", "Enable flag",     "1 = attach binary15m runner on boot", False),

    # binary15 (Kelly, prediction-market style)
    KeyDef("BINARY15_SIGNING_SECRET",  "Binary15",  "Signing secret",  "HMAC-SHA256 secret for the Kelly engine envelope", True),
    KeyDef("OPENCLAW_BINARY15",        "Binary15",  "Enable flag",     "1 = attach binary15 runner on boot", False),

    # Shared exchange/fee keys (kept — used by SPOT AGGRO + other runners).
    # The "Apex" group label was retired in phase-11n-9-t; these are now
    # under the neutral "Exchange" group (defined above).
    KeyDef("EXCHANGE_PASSPHRASE",         "Exchange", "Exchange passphrase",     "OKX/Coinbase style passphrase", True),
    KeyDef("SYMBOL",                      "Exchange", "Traded symbol",           "e.g. BTC-USDT-SWAP", False),
    KeyDef("CAPITAL_USD",                 "Exchange", "Account equity",          "USD figure sizing runs against", False),
    KeyDef("RISK_PER_TRADE_USD",          "Exchange", "Risk per trade",          "Dollars lost worst-case on stop-out", False),
    KeyDef("OKX_MAKER_FEE",               "Exchange", "Maker fee",               "Fraction (e.g. 0.0002)", False),
    KeyDef("OKX_TAKER_FEE",               "Exchange", "Taker fee",               "Fraction (e.g. 0.0005)", False),
    KeyDef("PANIC_SLIPPAGE_ESTIMATE",     "Exchange", "Panic slippage estimate", "Fraction assumed on panic exit", False),

    # Phase 11n-9-t — removed (deep-apex purge):
    #   Model 99-X Apex KeyDefs (OPENCLAW_99X_APEX, TARGET_ALPHA_BPS,
    #   STOP_LOSS_BPS, CVD_Z_SCORE_THRESHOLD, FUNDING_RATE_THRESHOLD,
    #   CVD_WINDOW_SIZE, EXCHANGE_ID compat).
    #   APEX V2 KeyDefs (APEX_V2_ENABLED, ACTIVE_STRATEGY, APEX_V2_SYMBOL,
    #   APEX_V2_CAPITAL, APEX_V2_LEVERAGE, APEX_V2_DEMO_MODE,
    #   APEX_V2_STATE_FILE).
    # API secrets + exchange passphrase preserved above per operator spec.

    # CoPI v2 (Aggressive Carry Optimization, alt-perp portfolio)
    KeyDef("OPENCLAW_COPI_V2",            "CoPIv2", "Enable flag",           "1 = attach CoPI v2 alt-portfolio runner", False),
    KeyDef("COPI_V2_PAPER_ONLY",          "CoPIv2", "Paper-only switch",     "1 = force paper regardless of CLAW_MODE (recommended during pilot)", False),
    KeyDef("COPI_V2_INSTRUMENTS",         "CoPIv2", "Instruments",           "Comma-separated OKX perp ids, e.g. INJ-USDT-SWAP,WIF-USDT-SWAP,OP-USDT-SWAP,APT-USDT-SWAP,PEPE-USDT-SWAP", False),
    KeyDef("COPI_V2_CAPITAL_USD",         "CoPIv2", "Total capital",         "USD notional for the entire portfolio (split equally across instruments)", False),
    KeyDef("COPI_V2_MIN_RATE",            "CoPIv2", "MIN_RATE",              "Minimum |funding| per 8h to harvest (e.g. 0.002 = 0.2%). Default 0.002", False),
    KeyDef("COPI_V2_T_CONFIRM",           "CoPIv2", "T_confirm",             "Consecutive fetches sign must persist. Default 1", False),
    KeyDef("COPI_V2_ALPHA_INITIAL",       "CoPIv2", "Initial alpha",         "Fraction of capital per strike. Default 0.15", False),
    KeyDef("COPI_V2_ALPHA_MAX",           "CoPIv2", "Max alpha",             "Cap on alpha. Default 0.30", False),
    KeyDef("COPI_V2_LEVERAGE",            "CoPIv2", "Leverage",              "Perp leverage. Default 7 (cap 12)", False),
    KeyDef("COPI_V2_BASE_COST",           "CoPIv2", "Base cost per event",   "Realistic per-event friction. Default 0.0015 (0.15%)", False),
    KeyDef("COPI_V2_MU_COLLAPSE_MULT",    "CoPIv2", "Mu collapse kill",      "Kill if mu_hat < this × hist_mu. Default 0.5", False),
    KeyDef("COPI_V2_DRAWDOWN_3D_PCT",     "CoPIv2", "3-day DD kill",         "Pause if 3d drawdown > pct. Default 0.10", False),
    KeyDef("COPI_V2_STATE_FILE",          "CoPIv2", "State file path",       "Crash-recovery JSON", False),
]


GROUP_ORDER = (
    "Ops", "Exchange", "LLM", "MarketData", "News", "Notify",
    "Binary15m", "Modes", "Infra",
)


def catalog_by_group() -> dict[str, list[KeyDef]]:
    out: dict[str, list[KeyDef]] = {}
    for k in CATALOG:
        out.setdefault(k.group, []).append(k)
    return out


# ---- masking --------------------------------------------------------------

def mask_value(v: Optional[str], *, sensitive: bool = True) -> Optional[str]:
    if v is None or v == "":
        return None
    if not sensitive:
        return v
    if len(v) <= 8:
        return "•" * len(v)
    return v[:4] + "•" * max(6, len(v) - 8) + v[-4:]


# ---- .env I/O -------------------------------------------------------------

_LINE_RE = re.compile(r"^\s*(?P<k>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<v>.*)$")


class SecretsStore:
    def __init__(self, env_file: Optional[str] = None, *, ledger: Optional[Any] = None):
        self.env_file = Path(env_file or os.environ.get("OPENCLAW_ENV_FILE", ".env"))
        self._lock = threading.Lock()
        self._ledger = ledger

    # ---- read ----

    def _read_lines(self) -> list[str]:
        if not self.env_file.exists():
            return []
        return self.env_file.read_text(encoding="utf-8").splitlines()

    def _parse(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in self._read_lines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _LINE_RE.match(line)
            if not m:
                continue
            v = m.group("v").strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            out[m.group("k")] = v
        return out

    def list(self) -> list[dict[str, Any]]:
        current = self._parse()
        out: list[dict[str, Any]] = []
        for k in CATALOG:
            val = current.get(k.name, os.environ.get(k.name, "") or "")
            out.append({
                "name": k.name,
                "group": k.group,
                "label": k.label,
                "purpose": k.purpose,
                "sensitive": k.sensitive,
                "required": k.required,
                "set": bool(val),
                "masked": mask_value(val, sensitive=k.sensitive),
                "source": "env_file" if k.name in current else ("process_env" if os.environ.get(k.name) else None),
            })
        return out

    def get_raw(self, name: str) -> Optional[str]:
        return self._parse().get(name) or os.environ.get(name) or None

    # ---- write ----

    def set(self, name: str, value: str) -> None:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            raise ValueError(f"invalid key name: {name!r}")
        with self._lock:
            lines = self._read_lines()
            found = False
            new_lines: list[str] = []
            for line in lines:
                m = _LINE_RE.match(line)
                if m and m.group("k") == name:
                    new_lines.append(f"{name}={_escape(value)}")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                # Append under a section header if the file is empty-ish.
                if new_lines and new_lines[-1].strip():
                    new_lines.append("")
                new_lines.append(f"{name}={_escape(value)}")
            self.env_file.parent.mkdir(parents=True, exist_ok=True)
            self.env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            try:
                os.chmod(self.env_file, 0o600)
            except OSError:
                pass
        self._audit("secret_set", name, severity="info")

    def delete(self, name: str) -> bool:
        with self._lock:
            lines = self._read_lines()
            new_lines = [
                l for l in lines
                if not (m := _LINE_RE.match(l)) or m.group("k") != name
            ]
            if len(new_lines) == len(lines):
                return False
            self.env_file.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
        self._audit("secret_delete", name, severity="warn")
        return True

    # ---- audit ----

    def _audit(self, phase: str, name: str, *, severity: str = "info") -> None:
        if self._ledger is None:
            return
        try:
            self._ledger.record(
                kind="secret",
                phase=phase,
                severity=severity,
                result={"name": name, "env_file": str(self.env_file)},
            )
        except Exception:
            pass


def _escape(v: str) -> str:
    if v == "":
        return ""
    if any(c in v for c in " \t#\"\n"):
        return '"' + v.replace('"', '\\"') + '"'
    return v
