"""Phase 11n-9-ss — Strategy scope guard for Contrarian + Deep Value panel.

Single point of truth for:
  - which strategy names this scoped panel serves
  - RBAC role validation
  - feature-flag check
  - strategy-scoped DB filters

Any panel endpoint MUST call one of:
  require_cdv_scope(row_dict)            raises ScopeViolation on leak
  scoped_variants_filter_sql()           returns SQL WHERE clause
  rbac_check(token)                      raises HTTPException on bad role

Never allow a function to return data without going through these.
"""
from __future__ import annotations

import os
from typing import Any

# The two strategies this panel is authorized to show.
CDV_STRATEGY_NAMESPACE = "contrarian_deepvalue"
CDV_VARIANTS: frozenset[str] = frozenset({"contrarian", "deep_value"})

# RBAC role IDs.
RBAC_VIEWER = "strategy:contrarian_deepvalue_viewer"
RBAC_ADMIN = "strategy:contrarian_deepvalue_admin"

# Feature flag — default OFF per spec. Operator flips to enable.
FEATURE_FLAG_ENV = "FEATURE_CONTRARIAN_DEEPVALUE_PANEL"


class ScopeViolation(Exception):
    """Raised when a non-CDV record reaches the scoped panel."""


def feature_enabled() -> bool:
    """Panel only renders/serves data when feature flag is on."""
    return os.environ.get(FEATURE_FLAG_ENV, "").strip() in ("1", "true", "on")


def require_cdv_scope(row: dict[str, Any]) -> None:
    """Raise ScopeViolation if a row references a variant outside CDV scope."""
    v = row.get("variant") or row.get("admitting_variant")
    if v is not None and v not in CDV_VARIANTS:
        raise ScopeViolation(
            f"row variant={v!r} outside CDV scope {sorted(CDV_VARIANTS)}"
        )


def filter_cdv_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop rows whose variant is outside CDV scope. Silent filter — not
    raise — because call sites may include multi-variant aggregates."""
    out: list[dict[str, Any]] = []
    for r in rows or []:
        v = r.get("variant") or r.get("admitting_variant")
        if v is None or v in CDV_VARIANTS:
            out.append(r)
    return out


def scoped_variants_sql_in_clause() -> str:
    """Returns an SQL fragment: variant IN ('contrarian','deep_value')."""
    vals = ",".join(f"'{v}'" for v in sorted(CDV_VARIANTS))
    return f"variant IN ({vals})"


def scoped_live_variants_env_value() -> str:
    """The exact SPOT_LIVE_VARIANTS env value this panel manages."""
    return ",".join(sorted(CDV_VARIANTS))


def rbac_has_role(headers_or_token: str | None, role_id: str) -> bool:
    """Minimal RBAC: operator sets X-CDV-Role header to one of the role
    IDs, OR passes an ops admin token that grants admin by default.

    For production this would resolve to real RBAC. For our scale we
    enforce a simple header-based model alongside the existing
    OPS_ADMIN_TOKEN fallback.
    """
    if not headers_or_token:
        return False
    # Admin token from existing infra grants admin-level access.
    admin_token = os.environ.get("OPS_ADMIN_TOKEN", "")
    if admin_token and headers_or_token == admin_token:
        return True
    # Explicit role string match.
    return headers_or_token == role_id or headers_or_token.startswith(role_id)


def rbac_require(role_id: str, token_or_header: str | None) -> None:
    """Raise HTTPException(403) if token does not grant role."""
    from fastapi import HTTPException
    if not feature_enabled():
        raise HTTPException(
            status_code=404,
            detail="contrarian_deepvalue panel disabled (feature flag off)",
        )
    if not rbac_has_role(token_or_header, role_id):
        raise HTTPException(
            status_code=403,
            detail=f"role required: {role_id}",
        )


def mask_non_cdv_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Defensive: strip fields known to carry non-CDV data."""
    if not isinstance(payload, dict):
        return payload
    stripped = dict(payload)
    # Drop any 'standings' entries outside CDV scope (horse race payloads).
    standings = stripped.get("standings")
    if isinstance(standings, list):
        stripped["standings"] = [
            s for s in standings if s.get("variant") in CDV_VARIANTS
        ]
    # Same for any 'variants' list.
    variants = stripped.get("variants")
    if isinstance(variants, list):
        stripped["variants"] = [
            v for v in variants if (
                v.get("variant") in CDV_VARIANTS
                or v.get("name") in CDV_VARIANTS
            )
        ]
    return stripped
