"""Phase 11h — engine must NOT auto-start on server boot.

Policy: uvicorn boot is purely a read-only-plus-control-plane action.
The SPOT AGGRO engine is instantiated only by the operator clicking
Resume on the dashboard (POST /spot_aggro/start with a valid
OPS_ADMIN_TOKEN).

This file locks that policy. An incident on 2026-04-19 confirmed why:
a SPOT_AGGRO_AUTO_ARM flag (intended to be off by default) was
inadvertently set in the shell environment during a server restart,
causing the engine to boot with dry_run=False and place 5 live Tier C
orders on OKX before the operator could intervene.

Regression guard:
  1. server.py has no @app.on_event("startup") hook that calls start_engine.
  2. server.py imports no function that instantiates SpotAggroEngine at boot.
  3. The specific SPOT_AGGRO_AUTO_ARM gate name must not be referenced
     as an active flag (only in the commemorative comment explaining
     why it was removed).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SERVER_PY = REPO / "openclaw_v1" / "server.py"


def _server_src() -> str:
    return SERVER_PY.read_text(encoding="utf-8")


def test_no_startup_hook_calls_start_engine():
    """No FastAPI startup event handler may call start_engine() — every
    engine start must be explicit operator action through the API."""
    src = _server_src()
    # Strip comments + docstrings so the commemorative history note that
    # mentions "start_engine" in prose doesn't trip this check.
    stripped = re.sub(r"#.*", "", src)
    stripped = re.sub(r'"""[\s\S]*?"""', "", stripped)
    # Now look for an actual call: start_engine( somewhere in live code.
    assert "start_engine(" not in stripped, (
        "server.py must not call start_engine() anywhere — the engine "
        "is resumed only by the operator via POST /spot_aggro/start"
    )


def test_no_startup_hook_instantiates_engine_class():
    """No FastAPI startup event handler may instantiate SpotAggroEngine
    directly either. Same rule as above, belt-and-suspenders."""
    src = _server_src()
    stripped = re.sub(r"#.*", "", src)
    stripped = re.sub(r'"""[\s\S]*?"""', "", stripped)
    assert "SpotAggroEngine(" not in stripped, (
        "server.py must not instantiate SpotAggroEngine — construction "
        "happens only inside spot_aggro.start_engine() triggered by "
        "explicit operator action"
    )
    # The legacy alias too.
    assert "APEX_Spot_Aggro(" not in stripped, (
        "server.py must not instantiate APEX_Spot_Aggro — same policy "
        "as SpotAggroEngine (they are the same class)"
    )


def test_spot_aggro_auto_arm_flag_is_not_active_code():
    """The SPOT_AGGRO_AUTO_ARM env-var gate may appear in COMMENTS as the
    history of why auto-start was removed. It must not appear in any
    active code line (if/when guard, _flag() call, os.environ.get, etc.)."""
    src = _server_src()
    stripped = re.sub(r"#.*", "", src)
    stripped = re.sub(r'"""[\s\S]*?"""', "", stripped)
    assert "SPOT_AGGRO_AUTO_ARM" not in stripped, (
        "SPOT_AGGRO_AUTO_ARM must not be referenced in executable code — "
        "the flag was removed after the 2026-04-19 incident; any future "
        "auto-start mechanism must require explicit operator approval"
    )


def test_commemorative_comment_explains_why():
    """Future maintainers must be able to read server.py and understand
    why no auto-start exists. The policy comment must explicitly mention
    both the incident and the replacement operator flow."""
    src = _server_src()
    assert "MUST NOT auto-start" in src
    assert "POST /spot_aggro/start" in src
    assert "2026-04-19" in src, (
        "server.py must name the incident date so the history is not lost"
    )
