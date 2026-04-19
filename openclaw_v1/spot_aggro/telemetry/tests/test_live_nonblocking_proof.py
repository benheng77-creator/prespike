"""
Integration proof: telemetry never blocks when infra is down.

This test runs a burst of 500 emits against a closed-port sink, then
measures wall-clock elapsed time. Must complete in well under 1s even
though every emit queues on a broken connection.
"""
from __future__ import annotations

import socket
import time

from spot_aggro.telemetry import emitter
from spot_aggro.telemetry.questdb_sink import QuestDBSink


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close(); return p


def test_live_burst_while_questdb_down(monkeypatch) -> None:
    """Full emitter path → sink → closed port. 500 emits under 1s."""
    sink = QuestDBSink("127.0.0.1", _free_port(), capacity=2048,
                       connect_timeout_s=0.1, send_timeout_s=0.1)
    sink.start()
    try:
        monkeypatch.setattr(emitter, "_sink", sink)
        t0 = time.monotonic()
        for i in range(500):
            emitter.emit_trade(
                "enter", symbol=f"SYM{i}-USDT", tier="B",
                module="M1_flow_B", notional_usd=20.0,
                composite=0.62, spi=0.55, correlation_id=f"cid-{i}",
            )
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"500 emits took {elapsed:.3f}s (infra down)"
    finally:
        sink.stop(0.5)
