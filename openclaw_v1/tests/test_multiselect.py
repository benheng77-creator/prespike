"""Tests for multi-select strategy selection."""

import json
import os
from pathlib import Path

import pytest


def test_parse_models_string_handles_aliases_and_duplicates():
    from launcher import parse_models_string
    assert parse_models_string("A, E") == ["A", "E"]
    assert parse_models_string("apex_v2,99x_apex,a") == ["E", "D", "A"]
    assert parse_models_string("A,A,A") == ["A"]
    assert parse_models_string("junk") == []
    assert parse_models_string("") == []


def test_resolve_active_models_prefers_cli_models_flag(monkeypatch):
    from launcher import resolve_active_models
    import argparse
    ns = argparse.Namespace(models="A,E", model=None, apex_v2=False, apex_v2_only=False)
    monkeypatch.setenv("ACTIVE_MODELS", "C")
    assert resolve_active_models(ns) == ["A", "E"]


def test_resolve_active_models_falls_back_to_env(monkeypatch):
    from launcher import resolve_active_models
    import argparse
    ns = argparse.Namespace(models=None, model=None, apex_v2=False, apex_v2_only=False)
    monkeypatch.setenv("ACTIVE_MODELS", "D,E")
    assert resolve_active_models(ns) == ["D", "E"]


def test_resolve_active_models_reads_persisted_file(monkeypatch, tmp_path):
    from launcher import resolve_active_models, SELECTION_FILE
    import argparse
    ns = argparse.Namespace(models=None, model=None, apex_v2=False, apex_v2_only=False)
    monkeypatch.delenv("ACTIVE_MODELS", raising=False)
    # Redirect SELECTION_FILE via monkeypatch
    fake = tmp_path / "active_models.json"
    fake.write_text(json.dumps({"models": ["A", "D"]}), encoding="utf-8")
    import launcher as _l
    monkeypatch.setattr(_l, "SELECTION_FILE", fake)
    assert resolve_active_models(ns) == ["A", "D"]


def test_save_and_load_selection_roundtrip(monkeypatch, tmp_path):
    import launcher as _l
    fake = tmp_path / "active_models.json"
    monkeypatch.setattr(_l, "SELECTION_FILE", fake)
    _l.save_selection_to_file(["A", "E"])
    assert _l.load_selection_from_file() == ["A", "E"]


def test_models_d_and_e_are_strictly_separate_packages():
    """Model D (99-X Apex) and Model E (APEX V2) must not share a package."""
    repo = Path(__file__).resolve().parent.parent
    assert (repo / "model_99_x_apex").is_dir()
    assert (repo / "apex_v2").is_dir()
    # No cross-imports from one into the other
    d_files = list((repo / "model_99_x_apex").rglob("*.py"))
    e_files = list((repo / "apex_v2").rglob("*.py"))
    for p in d_files:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        assert "apex_v2" not in txt, f"{p.name} references apex_v2 — must be independent"
    for p in e_files:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        assert "model_99_x_apex" not in txt, f"{p.name} references model_99_x_apex — must be independent"


def test_launcher_runners_D_and_E_are_distinct_callables():
    from launcher import RUNNERS
    assert RUNNERS["D"] is not RUNNERS["E"]
    assert RUNNERS["D"].__name__ == "run_model_D"
    assert RUNNERS["E"].__name__ == "run_model_E"


# ---- server endpoints ------------------------------------------------------

def test_server_selection_get_shape(tmp_path, monkeypatch):
    import server
    # Write a persisted selection into the expected runtime/ path
    persisted = Path(server.__file__).resolve().parent / "runtime" / "active_models.json"
    persisted.parent.mkdir(parents=True, exist_ok=True)
    persisted.write_text(json.dumps({"models": ["A", "E"]}), encoding="utf-8")
    monkeypatch.delenv("ACTIVE_MODELS", raising=False)
    r = server.models_selection_get()
    assert set(r["available"].keys()) == {"A", "B", "C", "D", "E"}
    assert "A" in r["selected_persisted"] and "E" in r["selected_persisted"]
    # cleanup
    persisted.unlink(missing_ok=True)


def test_server_selection_post_no_admin_ok(tmp_path, monkeypatch):
    import server
    monkeypatch.delenv("OPS_ADMIN_TOKEN", raising=False)
    r = server.models_selection_post({"models": ["a", "e", "ZZ"]})
    assert r["ok"] is True
    assert r["models"] == ["A", "E"]
    # cleanup
    persisted = Path(server.__file__).resolve().parent / "runtime" / "active_models.json"
    persisted.unlink(missing_ok=True)


def test_server_selection_post_requires_admin_when_set(monkeypatch):
    import server
    from fastapi import HTTPException
    monkeypatch.setenv("OPS_ADMIN_TOKEN", "secret-token")
    with pytest.raises(HTTPException) as ei:
        server.models_selection_post({"models": ["A"]})
    assert ei.value.status_code == 401


def test_server_selection_post_accepts_admin_when_set(monkeypatch):
    import server
    monkeypatch.setenv("OPS_ADMIN_TOKEN", "secret-token")
    r = server.models_selection_post({"models": ["A"], "admin_token": "secret-token"})
    assert r["ok"] is True
    assert r["models"] == ["A"]
    # cleanup
    persisted = Path(server.__file__).resolve().parent / "runtime" / "active_models.json"
    persisted.unlink(missing_ok=True)
