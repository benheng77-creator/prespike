"""
APEX-Omega SPOT AGGRO — 5-LLM prompt templates.
Squeeze-specific. Each model evaluates a different facet of the squeeze thesis.
"""

from __future__ import annotations
from typing import Any


CONTEXT_TEMPLATE = """ASSET: {symbol}-USDT on OKX spot
PRICE: ${price:.4f} | 24h: {ret_24h:+.2f}% | 7d: {ret_7d:+.2f}%
FUNDING (perp signal): {funding_rate:+.6f}% per 8h | z-score: {funding_z:.2f} | 30d-sigma: {funding_sigma:.4f}%
OI: ${oi_usd:.0f} | 24h change: {oi_change:+.1f}%
SPI: {spi:.3f} | Components: fz={spi_fz:.2f} oi={spi_oi:.2f} div={spi_div:.2f} liq={spi_liq:.2f}
BOOK: ${depth_5lvl:.0f} depth | spread: {spread_bp:.1f}bp
LIQD: ${liq_1h:.0f} perp liquidations past 1h
TIME: {timestamp} UTC"""

_JSON_SUFFIX = '\n\nRespond ONLY with JSON: {"score": <float -1..+1>, "confidence": <float 0..1>, "rationale": "<30 words max>"}'

ROLE_PROMPTS = {
    "pattern": (
        "You are a crypto squeeze-pattern detector. Your ONLY job: does this data "
        "match a historical short-squeeze setup?\n\n{context}\n\n"
        "Score: +1 classic squeeze (neg funding + rising OI + liq cluster near), "
        "-1 trap/manipulation, 0 ambiguous."
    ),
    "macro": (
        "You are a macro-event analyst. Your ONLY job: is there a macro catalyst "
        "that could accelerate OR block a short squeeze on this asset?\n\n{context}\n\n"
        "Score: +1 strong catalyst present, -1 blocking event, 0 neutral."
    ),
    "sentiment": (
        "You are a sentiment analyser. Your ONLY job: is social/market sentiment "
        "aligned with a squeeze thesis?\n\n{context}\n\n"
        "Score: +1 extreme fear + contrarian buy (max squeeze fuel), "
        "-1 euphoria (no shorts to squeeze), 0 mixed."
    ),
    "quant_sanity": (
        "You are a quant auditor. Verify the SPI calculation from raw data.\n\n"
        "{context}\n\n"
        "SPI = 0.35*max(0,-fz)/5 + 0.25*clip(oi_chg,0,0.20)/0.20 + "
        "0.25*clip(ret7d-f*1000,0,0.10)/0.10 + 0.15*liq_prox\n"
        "Score: +1 data consistent + SPI verified, -1 critical inconsistency."
    ),
    "outlier": (
        "You are a manipulation detector. Your ONLY job: is this squeeze signal "
        "organic or manufactured?\n\n{context}\n\n"
        "Score: +1 organic squeeze in favourable regime, "
        "-1 confirmed spoofing/wash/coordinated pump, 0 uncertain."
    ),
}

BLITZ_PROMPT = (
    "BLITZ MODE ACTIVE. Concentrated 60%-capital trade on post-cascade rebound. "
    "Maximum scrutiny.\n\n{context}\n\n"
    "Is the cascade GENUINELY exhausting or pausing before leg 2? "
    "Subtract 0.2 from your normal score (higher bar for BLITZ)."
)

VETO_PROMPT = (
    "ENSEMBLE CONFLICT > 0.70 on squeeze trade. Break the tie.\n\n"
    "{context}\n\nENSEMBLE:\n{ensemble_summary}\n\n"
    "VETO if 2+ models flag manipulation or data inconsistency. "
    "ALLOW if disagreement is about magnitude not direction.\n\n"
    'Respond ONLY with JSON: {{"veto": <true|false>, "confidence": <float>, '
    '"rationale": "<50 words>"}}'
)


def build_context(symbol: str, data: dict[str, Any]) -> str:
    return CONTEXT_TEMPLATE.format(
        symbol=symbol,
        price=data.get("price", 0),
        ret_24h=data.get("ret_24h", 0),
        ret_7d=data.get("ret_7d", 0),
        funding_rate=data.get("funding_rate", 0),
        funding_z=data.get("funding_z", 0),
        funding_sigma=data.get("funding_sigma", 0),
        oi_usd=data.get("oi_usd", 0),
        oi_change=data.get("oi_change", 0),
        spi=data.get("spi", 0),
        spi_fz=data.get("spi_fz", 0),
        spi_oi=data.get("spi_oi", 0),
        spi_div=data.get("spi_div", 0),
        spi_liq=data.get("spi_liq", 0),
        depth_5lvl=data.get("depth_5lvl", 0),
        spread_bp=data.get("spread_bp", 0),
        liq_1h=data.get("liq_1h", 0),
        timestamp=data.get("timestamp", ""),
    )


def build_ensemble_prompts(symbol: str, data: dict[str, Any],
                            is_blitz: bool = False) -> dict[str, str]:
    ctx = build_context(symbol, data)
    out = {}
    for role, tmpl in ROLE_PROMPTS.items():
        if is_blitz:
            prompt = BLITZ_PROMPT.replace("{context}", ctx) + _JSON_SUFFIX
        else:
            prompt = tmpl.replace("{context}", ctx) + _JSON_SUFFIX
        out[role] = prompt
    return out
