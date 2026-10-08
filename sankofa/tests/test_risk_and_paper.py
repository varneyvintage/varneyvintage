
from sankofa.data.market import Bar
from sankofa.execution.paper import PaperBroker
from sankofa.strategy.combiner import Target
from sankofa.strategy.risk import RiskManager
from sankofa.universe import load_universe
from tests.conftest import ROOT

RISK = {"risk_per_trade_pct": 4.0, "max_position_pct": 30.0, "max_gross_exposure_pct": 250.0,
        "max_net_exposure_pct": 180.0, "max_open_positions": 2, "atr_stop_multiple": 2.5,
        "daily_loss_kill_pct": 8.0, "max_drawdown_kill_pct": 30.0, "min_signal_conviction": 0.25}


def _uni():
    return load_universe(ROOT / "config" / "universe.yaml")


def test_sizing_respects_position_cap(tmp_path):
    rm = RiskManager(RISK, tmp_path / "r.json", 25_000)
    gfi = _uni().get("GFI")
    t = Target(gfi, 1.0, 1.0, {})
    units, stop = rm.desired_units(t, Bar(close=20.0, high=20.5, low=19.5, atr=0.2), 25_000)
    assert units > 0 and stop < 20.0
    assert units * 20.0 <= 25_000 * 0.30 + 20.0


def test_low_conviction_is_zero(tmp_path):
    rm = RiskManager(RISK, tmp_path / "r.json", 25_000)
    t = Target(_uni().get("GOLD"), 0.1, 0.1, {})
    assert rm.desired_units(t, Bar(2400, 2410, 2390, 30), 25_000) == (0.0, None)


def test_portfolio_caps_limit_count_and_gross(tmp_path):
    rm = RiskManager(RISK, tmp_path / "r.json", 10_000)
    desired = {"A": (100.0, 100.0), "B": (50.0, 100.0), "C": (10.0, 100.0)}  # notionals 10k, 5k, 1k
    capped = rm.portfolio_caps(desired, 10_000)
    assert set(capped) == {"A", "B"}
    gross = sum(abs(capped[k] * desired[k][1]) for k in capped)
    assert gross <= 10_000 * 2.5 + 1e-6


def test_drawdown_kill_switch(tmp_path):
    rm = RiskManager(RISK, tmp_path / "r.json", 10_000)
    rm.update_equity(10_000)
    rm.update_equity(6_900)
    assert rm.state.halted and "drawdown" in rm.state.halt_reason


def test_paper_broker_round_trip_and_persistence(tmp_path):
    uni = _uni()
    p = tmp_path / "acct.json"
    b = PaperBroker(p, 10_000, slippage_bps=0)
    gfi = uni.get("GFI")
    b.mark({"GFI": 20.0})
    b.trade_to(gfi, 100, 20.0, 18.0, "open")
    b.mark({"GFI": 25.0})
    assert abs(b.equity() - 10_500) < 1e-6
    b.trade_to(gfi, 0, 25.0, None, "close")
    assert abs(b.cash - 10_500) < 1e-6 and b.positions() == {}
    b2 = PaperBroker(p, 10_000)
    assert abs(b2.cash - 10_500) < 1e-6


def test_paper_stop_is_enforced(tmp_path):
    uni = _uni()
    b = PaperBroker(tmp_path / "acct.json", 10_000, slippage_bps=0)
    cocoa = uni.get("COCOA")
    b.mark({"COCOA": 7000.0})
    b.trade_to(cocoa, 1, 7000.0, 6800.0, "open")
    b.mark({"COCOA": 6750.0})
    fills = b.check_stops(uni.get)
    assert len(fills) == 1 and b.positions() == {}
    assert abs(b.cash - (10_000 - 250 * 10)) < 1e-6  # cocoa multiplier 10
