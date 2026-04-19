"""Phase 11g — engine class rename regression tests.

Locks the rename of ``APEX_Spot_Aggro`` -> ``SpotAggroEngine``:

  1. ``SpotAggroEngine`` is the canonical class name and is importable.
  2. ``APEX_Spot_Aggro`` remains available as a backward-compat alias
     pointing at the same class (so any external importer / notebook /
     unpushed branch keeps working during rollout).
  3. The spot package's public ``start_engine`` uses the new name.
  4. The two pre-existing tests that introspect the engine class now
     reference the new name (not the alias) — so the alias can be
     removed in a future cleanup without silently turning those tests
     into no-ops.
  5. The stale URL inside ``forensic_v2/pdf_renderer.py`` has been
     corrected from ``/apex/spot_aggro/forensic_v2/`` to the real
     spot-owned route ``/spot_aggro/forensic_v2/``.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Option 4 — class rename
# ---------------------------------------------------------------------------

def test_spot_aggro_engine_class_exists():
    """The canonical class name is ``SpotAggroEngine``."""
    from spot_aggro.engine import SpotAggroEngine
    assert isinstance(SpotAggroEngine, type), "SpotAggroEngine must be a class"
    # Must be directly instantiable in dry_run mode.
    eng = SpotAggroEngine(dry_run=True)
    assert eng.dry_run is True


def test_legacy_alias_still_points_at_same_class():
    """The backward-compat alias ``APEX_Spot_Aggro`` must resolve to the
    same class object as ``SpotAggroEngine`` — not a separate class, not
    a subclass, not a stub."""
    from spot_aggro.engine import SpotAggroEngine, APEX_Spot_Aggro
    assert APEX_Spot_Aggro is SpotAggroEngine, (
        "APEX_Spot_Aggro alias must resolve to the exact same class "
        "object as SpotAggroEngine — otherwise callers that still use "
        "the legacy name will see divergent behavior"
    )


def test_package_init_imports_new_name():
    """``spot_aggro/__init__.py`` must import ``SpotAggroEngine`` (not
    the legacy alias) so new code reads the canonical name."""
    src = (REPO / "openclaw_v1" / "spot_aggro" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "from .engine import SpotAggroEngine" in src, (
        "package __init__ must import SpotAggroEngine by canonical name"
    )
    # The legacy import form must be gone from the package bootstrap —
    # anyone still using it goes through the alias in engine.py, not
    # through __init__.
    assert "from .engine import APEX_Spot_Aggro" not in src


def test_start_engine_instantiates_new_class_name():
    """The public entry point ``start_engine`` constructs the engine by
    the canonical name so reading this function tells the operator
    exactly which symbol is in use."""
    src = (REPO / "openclaw_v1" / "spot_aggro" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "_engine_instance = SpotAggroEngine(dry_run=dry_run)" in src


def test_start_engine_singleton_type_annotation_is_new_name():
    """Type annotation for the module-level singleton must be the new name."""
    src = (REPO / "openclaw_v1" / "spot_aggro" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "Optional[SpotAggroEngine]" in src


# ---------------------------------------------------------------------------
# Option 3 — forensic_v2 stale URL fix (lock was lifted for this one edit)
# ---------------------------------------------------------------------------

def test_forensic_v2_pdf_footer_uses_spot_owned_route():
    """Before this pass, the PDF footer printed
    ``Raw JSON: /apex/spot_aggro/forensic_v2/{id}`` — a URL prefix that
    was retired in Phase 10 when spot routes moved off ``/apex/*``. The
    PDF now prints the real spot-owned path."""
    src = (REPO / "openclaw_v1" / "spot_aggro" / "forensic_v2" /
           "pdf_renderer.py").read_text(encoding="utf-8")
    assert "/spot_aggro/forensic_v2/" in src, (
        "PDF footer must reference the spot-owned route"
    )
    assert "/apex/spot_aggro/forensic_v2/" not in src, (
        "retired /apex/spot_aggro/forensic_v2/ prefix must be gone"
    )
