"""Verify SHA-256 chain across daily audit log files."""
from __future__ import annotations

import json
from pathlib import Path

from ..services.audit_log import AuditLog


def test_chain_built_correctly(tmp_path):
    # Simulate 3 daily files
    log_root = tmp_path / "audit"
    log_root.mkdir()
    # Day 1
    f1 = log_root / "2025-01-01.jsonl"
    f1.write_bytes(b'{"x":1}\n{"x":2}\n')
    import hashlib
    h1 = hashlib.sha256(f1.read_bytes()).hexdigest()
    # Day 2 with chain header
    f2 = log_root / "2025-01-02.jsonl"
    header = json.dumps({"_chain_prev_sha256": h1, "_chain_prev_file": "2025-01-01.jsonl"})
    f2.write_bytes((header + "\n").encode() + b'{"y":1}\n')
    h2 = hashlib.sha256(f2.read_bytes()).hexdigest()
    # Day 3 with chain header
    f3 = log_root / "2025-01-03.jsonl"
    header = json.dumps({"_chain_prev_sha256": h2, "_chain_prev_file": "2025-01-02.jsonl"})
    f3.write_bytes((header + "\n").encode() + b'{"z":1}\n')

    ok, errors = AuditLog.verify_chain(log_root)
    assert ok, errors


def test_chain_detects_tampering(tmp_path):
    log_root = tmp_path / "audit"
    log_root.mkdir()
    f1 = log_root / "2025-01-01.jsonl"
    f1.write_bytes(b'{"x":1}\n')
    import hashlib
    h1 = hashlib.sha256(f1.read_bytes()).hexdigest()
    f2 = log_root / "2025-01-02.jsonl"
    header = json.dumps({"_chain_prev_sha256": h1, "_chain_prev_file": "2025-01-01.jsonl"})
    f2.write_bytes((header + "\n").encode() + b'{"y":1}\n')
    # TAMPER with day 1
    f1.write_bytes(b'{"x":1, "TAMPERED": true}\n')
    ok, errors = AuditLog.verify_chain(log_root)
    assert not ok
    assert any("hash mismatch" in e for e in errors)
