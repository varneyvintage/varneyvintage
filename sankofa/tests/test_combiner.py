from sankofa.signals.base import Signal, SignalSet
from sankofa.strategy.combiner import Combiner
from sankofa.universe import AfricaLink, Instrument, Universe, load_universe
from tests.conftest import ROOT

W = {"weights": {"copytrade": 0.5, "sentiment": 0.25, "macro": 0.25},
     "stale_after_minutes": {"copytrade": 60, "sentiment": 60, "macro": 60}}
E = {"african_linkage_required": True, "producer_aligned_long_boost": 1.25,
     "no_short_african_equities": True, "no_short_african_currencies": True,
     "no_long_staples_on_food_shock_sentiment": True, "staple_ids": ["MAIZE", "WHEAT"]}


def _uni():
    return load_universe(ROOT / "config" / "universe.yaml")


def _targets(sigs):
    s = SignalSet()
    s.add(*sigs)
    return {t.instrument.id: t for t in Combiner(_uni(), W, E).combine(s)}


def test_missing_sources_redistribute_weight():
    t = _targets([Signal("copytrade", "COPPER", 0.8)])
    # only one source -> its weight is 100%, so raw == score*conf
    assert abs(t["COPPER"].raw - 0.8) < 1e-9


def test_producer_aligned_long_is_boosted():
    t = _targets([Signal("copytrade", "COCOA", 0.4)])
    assert abs(t["COCOA"].conviction - 0.5) < 1e-9
    assert any("producer-aligned" in n for n in t["COCOA"].ethos_notes)


def _synthetic_universe():
    link = AfricaLink(("ZA",), "test", True)
    return Universe({
        "GFI": Instrument("GFI", "miner", "equity", "GFI", {}, link, ()),
        "USDZAR": Instrument("USDZAR", "rand", "fx", "ZAR=X", {}, AfricaLink(("ZA",), "t", False), ()),
    }, {})


def test_equity_and_currency_rules_still_work_if_universe_grows():
    """The shipped universe is commodities-only, but the rules stay available."""
    uni = _synthetic_universe()
    def targets(sigs):
        s = SignalSet()
        s.add(*sigs)
        return {t.instrument.id: t for t in Combiner(uni, W, E).combine(s)}
    assert targets([Signal("copytrade", "GFI", -0.9)])["GFI"].conviction == 0.0
    assert targets([Signal("copytrade", "USDZAR", 0.9)])["USDZAR"].conviction == 0.0
    assert targets([Signal("copytrade", "USDZAR", -0.9)])["USDZAR"].conviction < 0


def test_food_shock_sentiment_leg_removed_on_staples():
    sent = Signal("sentiment", "MAIZE", 0.8, evidence={"food_shock_mentions": 3})
    copy = Signal("copytrade", "MAIZE", 0.4)
    t = _targets([sent, copy])
    # copytrade leg (0.5/0.75 * 0.4) survives, sentiment leg (0.25/0.75*0.8) stripped
    assert abs(t["MAIZE"].conviction - (0.5 / 0.75 * 0.4)) < 1e-9
    assert any("food-shock" in n for n in t["MAIZE"].ethos_notes)
