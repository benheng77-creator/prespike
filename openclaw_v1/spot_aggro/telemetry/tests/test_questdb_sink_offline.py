"""
QuestDB sink — offline behavior, queue semantics, non-blocking contract.

These tests MUST NOT depend on a live QuestDB. They verify:
  - emit() never blocks even when the target is down
  - queue full → drop-oldest + count
  - malformed input → counted as dropped, never raises
  - stop() drains cleanly within timeout
  - reconnect loop doesn't busy-spin
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from spot_aggro.telemetry.questdb_sink import QuestDBSink


def _find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_emit_is_non_blocking_when_target_unreachable() -> None:
    """Point at a closed port. emit must return quickly even while the
    background worker is stuck in its connect retry loop."""
    port = _find_free_port()
    sink = QuestDBSink("127.0.0.1", port, capacity=128,
                       connect_timeout_s=0.1, send_timeout_s=0.1)
    sink.start()
    try:
        started = time.monotonic()
        for i in range(200):
            sink.emit("spot_trades", {"action": "enter", "symbol": f"X{i}"},
                      {"notional_usd": 10.0}, time.time_ns())
        elapsed = time.monotonic() - started
        # 200 emits must complete in well under a second regardless of
        # the network state.
        assert elapsed < 1.0, f"emit blocked for {elapsed:.3f}s"
    finally:
        sink.stop(timeout_s=0.5)


def test_queue_full_drops_oldest_and_counts() -> None:
    port = _find_free_port()
    sink = QuestDBSink("127.0.0.1", port, capacity=4,
                       connect_timeout_s=0.05, send_timeout_s=0.05)
    # Do NOT start worker — this keeps the queue from being drained so we
    # can observe overflow deterministically.
    for i in range(20):
        sink.emit("spot_trades", {"action": "enter", "symbol": f"X{i}"},
                  {"notional_usd": float(i)}, time.time_ns())
    stats = sink.get_stats()
    # 20 pushes, capacity 4 → at least 16 drops on overflow.
    assert stats["dropped"] >= 16, stats


def test_malformed_emit_counts_as_dropped_not_raises() -> None:
    port = _find_free_port()
    sink = QuestDBSink("127.0.0.1", port, capacity=16,
                       connect_timeout_s=0.05, send_timeout_s=0.05)
    # Build a row that will blow up line builder: non-serializable value
    # passed through; we simulate by giving a value that raises on str().
    class Boom:
        def __repr__(self): raise RuntimeError("boom")
        def __str__(self): raise RuntimeError("boom")
    before = sink.get_stats()["dropped"]
    # Must not raise
    sink.emit("spot_trades", {"symbol": Boom()}, {"notional_usd": 1.0},
              time.time_ns())
    after = sink.get_stats()["dropped"]
    assert after == before + 1


def test_stop_is_idempotent_and_fast() -> None:
    port = _find_free_port()
    sink = QuestDBSink("127.0.0.1", port, capacity=8,
                       connect_timeout_s=0.1, send_timeout_s=0.1)
    sink.start()
    t0 = time.monotonic()
    sink.stop(timeout_s=1.0)
    sink.stop(timeout_s=1.0)   # double-stop tolerated
    assert (time.monotonic() - t0) < 2.0


def test_live_send_with_fake_server() -> None:
    """Spin a toy TCP echo on a local port. Emits should land as line
    bytes. This is the only test that exercises real socket I/O, all
    via localhost in-process."""
    host, port = "127.0.0.1", _find_free_port()
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.bind((host, port))
    lsock.listen(1)
    received: list[bytes] = []
    accepted = threading.Event()

    def server():
        conn, _ = lsock.accept()
        accepted.set()
        conn.settimeout(2.0)
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                received.append(data)
        except OSError:
            pass
        finally:
            try: conn.close()
            except Exception: pass

    t = threading.Thread(target=server, daemon=True)
    t.start()

    sink = QuestDBSink(host, port, capacity=64,
                       connect_timeout_s=1.0, send_timeout_s=1.0)
    sink.start()
    try:
        assert accepted.wait(timeout=2.0), "server did not accept"
        sink.emit("spot_trades", {"action": "enter", "symbol": "INJ-USDT"},
                  {"notional_usd": 20.0}, ts_ns=1_700_000_000_000_000_000)
        # Allow worker to send.
        time.sleep(0.3)
        blob = b"".join(received)
        assert b"spot_trades" in blob
        assert b"action=enter" in blob
        assert b"symbol=INJ-USDT" in blob
        assert b"notional_usd=20.0" in blob
        assert sink.get_stats()["sent"] >= 1
    finally:
        sink.stop(timeout_s=1.0)
        try: lsock.close()
        except Exception: pass


def test_escape_special_chars_in_tags_and_strings() -> None:
    """Commas, equals, spaces in tag values must be escaped per ILP spec.
    String field values must be quoted and escape backslash + quote."""
    line = QuestDBSink._build_line(
        "spot_trades",
        {"symbol": "X,Y=Z W", "action": "enter"},
        {"reason": "it said \"no\" to us\\back"},
        ts_ns=1,
    )
    text = line.decode("utf-8")
    # Escaped comma/equals/space in tag
    assert "symbol=X\\,Y\\=Z\\ W" in text
    # Quoted string with escaped backslash+quote
    assert '"it said \\"no\\" to us\\\\back"' in text
    # Trailing timestamp and newline
    assert text.endswith(" 1\n")
