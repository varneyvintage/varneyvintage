from sankofa.signals.sentiment import SentimentScorer, TextItem, is_food_shock, tag_instruments
from sankofa.universe import load_universe
from tests.conftest import ROOT


def _uni():
    return load_universe(ROOT / "config" / "universe.yaml")


def test_keyword_tagging_maps_to_instruments():
    idx = _uni().keyword_index()
    assert "COCOA" in tag_instruments("Ghana Cocobod raises farmgate price as cocoa rallies", idx)
    assert "PLATINUM" in tag_instruments("Implats warns Eskom load shedding will hit PGM output", idx)
    assert tag_instruments("Nothing relevant here about tennis", idx) == set()


def test_short_keyword_needs_word_boundary():
    idx = _uni().keyword_index()
    # 'zar' must not match inside 'bazaar'
    assert "USDZAR" not in tag_instruments("the bazaar was busy", idx)


def test_supply_shock_pushes_commodity_score_up():
    sc = SentimentScorer(_uni(), "vader", min_mentions=1)
    items = [TextItem("Cocoa shortage worsens as swollen shoot disease hits Ivory Coast farms", "t")] * 2
    sigs = {s.instrument_id: s for s in sc.score_items(items)}
    assert sigs["COCOA"].score > 0  # bad news for farmers, bullish for price


def test_min_mentions_filters_thin_evidence():
    sc = SentimentScorer(_uni(), "vader", min_mentions=3)
    sigs = sc.score_items([TextItem("gold rallies in Ghana", "t")])
    assert sigs == []


def test_food_shock_flag_is_recorded():
    assert is_food_shock("Famine warning as maize harvest fails in southern Africa drought")
    sc = SentimentScorer(_uni(), "vader", min_mentions=1)
    sigs = {s.instrument_id: s for s in sc.score_items(
        [TextItem("Famine warning as maize harvest fails in southern Africa drought", "t")])}
    assert sigs["MAIZE"].evidence["food_shock_mentions"] == 1


def test_bumper_harvest_is_bearish_for_price():
    sc = SentimentScorer(_uni(), "vader", min_mentions=1)
    sigs = {s.instrument_id: s for s in sc.score_items(
        [TextItem("Ivory Coast cocoa farmers celebrate record harvest and bumper crop", "t")])}
    assert sigs["COCOA"].score < 0


def test_equity_sentiment_is_not_inverted():
    sc = SentimentScorer(_uni(), "vader", min_mentions=1)
    sigs = {s.instrument_id: s for s in sc.score_items(
        [TextItem("Gold Fields shares slump after strike disruption at Tarkwa mine", "t")])}
    assert sigs["GFI"].score < 0           # equity: bad news is bad
    assert sigs["GOLD"].score > 0          # the metal: supply disruption is bullish
