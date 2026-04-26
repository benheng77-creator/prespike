"""
SHA-256 chained, append-only audit log.

Speed posture: append-only writes, line-buffered. No per-write fsync to
keep latency low; daily file rotation flushes on close. Chain integrity
is verified offline by scripts/verify_audit_chain.py.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import orjson as _json
    def _dumps(o) -> bytes: return _json.dumps(o)
except ImportError:  # fallback
    import json as _stdjson
    def _dumps(o) -> bytes: return _stdjson.dumps(o, separators=(",", ":")).encode()


@dataclass(slots=True)
class AuditLogEntry:
    ts_iso: str
    decision: str               # "approved" | "rejected"
    strategy_id: str
    instrument: str
    audit_token: str | None
    failed_checks: list[str]
    reasons: list[str]
    recomputed_p: float | None
    feature_drift: float | None
    p_spike_drift: float | None
    provenance_summary: dict


class AuditLog:
    """Append-only daily-rotated log with SHA-256 chaining across days.

    File format: each file is a JSONL stream. The FIRST line of each daily
    file (except the first ever) is a special header:
        {"_chain_prev_sha256": "<hex>", "_chain_prev_file": "<filename>"}
    All subsequent lines are AuditLogEntry JSON objects.

    Within a file, integrity is provided by the chain header pointing back
    to the prior file's terminal SHA-256. Tampering with any past file
    breaks the chain on the NEXT file's load.
    """

    def __init__(self, log_root: str | Path):
        self.log_root = Path(log_root)
        self.log_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fp = None
        self._current_date = None

    # ------------------------------------------------------------------
    def _file_for(self, date_iso: str) -> Path:
        return self.log_root / f"{date_iso}.jsonl"

    def _previous_file(self, date_iso: str) -> Path | None:
        existing = sorted(p for p in self.log_root.glob("*.jsonl"))
        existing = [p for p in existing if p.stem < date_iso]
        return existing[-1] if existing else None

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _open_for_today(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._current_date == today and self._fp is not None:
            return
        if self._fp is not None:
            try:
                self._fp.flush()
                self._fp.close()
            except Exception:
                pass
            self._fp = None
        path = self._file_for(today)
        new_file = not path.exists()
        # Open in APPEND-ONLY binary mode. We never seek, never rewrite.
        self._fp = open(path, "ab", buffering=0)
        self._current_date = today
        if new_file:
            prev = self._previous_file(today)
            if prev is not None:
                header = {
                    "_chain_prev_sha256": self._sha256_file(prev),
                    "_chain_prev_file": prev.name,
                    "_chain_ts_iso": datetime.now(timezone.utc).isoformat(),
                }
                self._fp.write(_dumps(header) + b"\n")

    # ------------------------------------------------------------------
    def write(self, entry: AuditLogEntry) -> None:
        line = _dumps({
            "ts_iso": entry.ts_iso,
            "decision": entry.decision,
            "strategy_id": entry.strategy_id,
            "instrument": entry.instrument,
            "audit_token": entry.audit_token,
            "failed_checks": entry.failed_checks,
            "reasons": entry.reasons,
            "recomputed_p": entry.recomputed_p,
            "feature_drift": entry.feature_drift,
            "p_spike_drift": entry.p_spike_drift,
            "provenance_summary": entry.provenance_summary,
        }) + b"\n"
        with self._lock:
            self._open_for_today()
            self._fp.write(line)

    def close(self) -> None:
        with self._lock:
            if self._fp is not None:
                try:
                    self._fp.flush()
                    self._fp.close()
                except Exception:
                    pass
                self._fp = None

    # ------------------------------------------------------------------
    @classmethod
    def verify_chain(cls, log_root: str | Path) -> tuple[bool, list[str]]:
        """Verify the SHA-256 chain across all daily files. Returns (ok, errors)."""
        log_root = Path(log_root)
        files = sorted(log_root.glob("*.jsonl"))
        errors: list[str] = []
        if not files:
            return True, errors
        prev_path: Path | None = None
        prev_hash: str | None = None
        for path in files:
            with open(path, "rb") as f:
                first = f.readline()
            if prev_path is not None:
                # Expect a chain header
                try:
                    if first.startswith(b"{") and b"_chain_prev_sha256" in first:
                        try:
                            import json as _j
                            header = _j.loads(first)
                        except Exception:
                            errors.append(f"{path.name}: malformed chain header")
                            continue
                        if header.get("_chain_prev_file") != prev_path.name:
                            errors.append(
                                f"{path.name}: header references "
                                f"{header.get('_chain_prev_file')!r}, "
                                f"expected {prev_path.name!r}"
                            )
                        if header.get("_chain_prev_sha256") != prev_hash:
                            errors.append(
                                f"{path.name}: chain hash mismatch — "
                                f"file may have been tampered with"
                            )
                    else:
                        errors.append(f"{path.name}: missing chain header")
                except Exception as e:
                    errors.append(f"{path.name}: header parse error: {e}")
            prev_path = path
            prev_hash = cls._sha256_file(path)
        return (len(errors) == 0), errors


def utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
