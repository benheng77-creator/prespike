"""Unit tests for the RiskEngine circuit breakers + Kelly sizing."""

import os
import tempfile

from core.risk import RiskEngine


def _fresh(balance: float = 10_000.0, **kwargs) -> RiskEngine:
    return RiskEngine(starting_balance=balance, **kwargs)


def test_initial_state_not_halted():
    r = _fresh()
    assert r.check_halt(10_000.0) is None
    assert r.halted is False


def test_consecutive_losses_trip_halt():
    r = _fresh(max_consecutive_losses=3)
    for _ in range(3):
        r.on_trade_close(-50.0)
    assert r.check_halt(10_000.0) is not None
    assert r.halted is True
    assert "consecutive" in r.halt_reason


def test_winning_trade_resets_consecutive_losses():
    r = _fresh(max_consecutive_losses=3)
    r.on_trade_close(-50.0)
    r.on_trade_close(-50.0)
    r.on_trade_close(100.0)
    assert r.consecutive_losses == 0
    for _ in range(2):
        r.on_trade_close(-50.0)
    assert r.check_halt(10_000.0) is None


def test_daily_loss_trips_halt():
    r = _fresh(max_daily_loss=0.03)
    r.on_tick(10_000.0, "2026-04-10")
    assert r.check_halt(9_700.0) is None  # exactly at cap, OK
    assert r.check_halt(9_600.0) is not None  # past cap
    assert "daily loss" in r.halt_reason


def test_daily_loss_resets_on_new_day():
    r = _fresh(max_daily_loss=0.03)
    r.on_tick(10_000.0, "2026-04-10")
    r.check_halt(9_900.0)
    r.on_tick(9_900.0, "2026-04-11")
    assert r.session_start_balance == 9_900.0
    assert r.check_halt(9_700.0) is None  # 2% loss on new day


def test_drawdown_trips_halt():
    r = _fresh(max_drawdown=0.05)
    r.on_tick(10_000.0, "2026-04-10")
    r.on_tick(11_000.0, "2026-04-10")  # new peak
    assert r.peak_balance == 11_000.0
    # From peak 11000, a 5% drawdown = 10450
    assert r.check_halt(10_500.0) is None
    assert r.check_halt(10_400.0) is not None
    assert "drawdown" in r.halt_reason


def test_halt_persists_until_resume():
    r = _fresh(max_consecutive_losses=1)
    r.on_trade_close(-10.0)
    r.check_halt(10_000.0)
    assert r.halted is True
    r.resume()
    assert r.halted is False
    assert r.halt_reason == ""


def test_halt_file_external_killswitch():
    with tempfile.TemporaryDirectory() as tmp:
        halt_file = os.path.join(tmp, ".halt")
        r = _fresh(halt_file_path=halt_file)
        assert r.check_halt(10_000.0) is None
        # Create the halt file
        with open(halt_file, "w") as f:
            f.write("")
        assert r.check_halt(10_000.0) is not None
        assert "halt file" in r.halt_reason


def test_kelly_size_returns_zero_when_halted():
    r = _fresh()
    r._halt("test halt")
    assert r.kelly_size_quote(0.7, 2.0, 10_000.0) == 0.0


def test_kelly_size_returns_zero_for_negative_ev():
    r = _fresh()
    # win_prob=0.3, rr=1.0 → kelly = 0.3 - 0.7/1.0 = -0.4
    assert r.kelly_size_quote(0.3, 1.0, 10_000.0) == 0.0


def test_kelly_size_returns_positive_for_good_setup():
    r = _fresh()
    # win_prob=0.6, rr=2.0 → kelly = 0.6 - 0.4/2.0 = 0.4
    size = r.kelly_size_quote(0.6, 2.0, 10_000.0)
    assert abs(size - 4_000.0) < 1e-6


def test_kelly_size_zero_on_zero_balance():
    r = _fresh()
    assert r.kelly_size_quote(0.6, 2.0, 0.0) == 0.0


def test_kelly_size_rejects_exposure_over_leverage():
    r = _fresh(max_leverage=2.0)
    # current_exposure 25000 / balance 10000 = 2.5x > 2.0x cap
    assert r.kelly_size_quote(0.6, 2.0, 10_000.0, current_exposure=25_000.0) == 0.0
