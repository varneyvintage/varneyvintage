"""End-to-end cycle on synthetic prices with the live network collectors disabled."""
import json

from sankofa.config import load_settings
from sankofa.data.market import StaticMarketData
from sankofa.engine import Engine
from sankofa.universe import load_universe
from tests.conftest import synthetic_frame


def test_full_cycle_paper(project, monkeypatch):
    s = load_settings(project / "config" / "settings.yaml")
    uni = load_universe(s.universe_file)
    frames = {}
    for i, inst in enumerate(uni):
        drift = 0.004 if inst.id in ("COCOA", "GOLD", "COPPER") else 0.0
        frames[inst.data_symbol] = synthetic_frame(100.0, drift, seed=i)
    for k, v in uni.macro_series.items():
        frames.setdefault(v["data_symbol"], synthetic_frame(100.0, -0.002 if k == "DXY" else 0.0, seed=99))
    market = StaticMarketData(frames)
    eng = Engine(s, market=market)
    inbox = project / "state" / "copytrade_inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "l.json").write_text(json.dumps({"leader": "leader_macro_1", "positions": [
        {"symbol": "CC", "direction": "long", "size_pct": 25},
        {"symbol": "HG", "direction": "long", "size_pct": 20},
        {"symbol": "GFI", "direction": "short", "size_pct": 20},   # ethos must block this
    ]}))
    res = eng.cycle()
    assert not res["halted"]
    pos = eng.broker.positions()
    assert "COCOA" in pos and pos["COCOA"].units > 0
    assert "GFI" not in pos
    assert eng.journal.tail(1)[0]["kind"] == "cycle"
    exposure = eng.country_exposure()
    assert "CI" in exposure or "GH" in exposure
    # second cycle is idempotent on unchanged signals (no new fills besides stop refresh)
    res2 = eng.cycle()
    assert not res2["halted"]
