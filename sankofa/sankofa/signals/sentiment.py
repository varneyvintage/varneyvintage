"""Sentiment: turns a pile of text items into per-instrument scores.

Engine 'vader' is a free lexicon model (no download, no GPU). It is tuned for social text,
not commodity news, so treat its output as a coarse mood gauge. 'finbert' is optional.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from ..universe import Universe
from .base import Signal, clip, utcnow

log = logging.getLogger(__name__)

# Phrases that imply a food-supply shock. Used by the ethos rule that refuses to go long
# staples on the back of hunger headlines.
FOOD_SHOCK_TERMS = (
    "famine", "food crisis", "food insecurity", "hunger", "crop failure", "harvest failure",
    "drought", "locust", "food shortage", "starvation", "malnutrition",
)

# Domain tweaks: VADER does not know that "drought" is bullish for a crop's PRICE.
# Direction words -> (price direction) for supply-side commodities. Applied on top of VADER.
SUPPLY_BULLISH = ("shortage", "deficit", "drought", "strike", "disruption", "outage", "ban",
                  "export ban", "crop disease", "swollen shoot", "load shedding", "pipeline",
                  "sanction", "output cut", "production cut", "lower output", "smuggling",
                  "harmattan", "flooding", "el nino", "el niño")
SUPPLY_BEARISH = ("bumper", "record harvest", "surplus", "glut", "oversupply", "higher output",
                  "production rise", "output rise", "weak demand", "demand slump", "stockpile",
                  "inventories rise")


@dataclass
class TextItem:
    text: str
    source: str                       # rss:<domain> | gnews:<query> | x:<handle>
    published: datetime = field(default_factory=utcnow)
    weight: float = 1.0               # boosted accounts etc.
    url: str = ""


class VaderEngine:
    def __init__(self) -> None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        self._a = SentimentIntensityAnalyzer()

    def score(self, text: str) -> float:
        return float(self._a.polarity_scores(text)["compound"])


class FinBertEngine:  # pragma: no cover - optional heavy dependency
    def __init__(self) -> None:
        from transformers import pipeline

        self._p = pipeline("text-classification", model="ProsusAI/finbert", truncation=True)

    def score(self, text: str) -> float:
        out = self._p(text[:512])[0]
        sign = {"positive": 1.0, "negative": -1.0}.get(out["label"].lower(), 0.0)
        return sign * float(out["score"])


def make_engine(name: str):
    name = (name or "vader").lower()
    if name == "finbert":
        try:
            return FinBertEngine()
        except Exception as exc:
            log.warning("finbert unavailable (%s); falling back to vader", exc)
    return VaderEngine()


def commodity_price_score(base: float, text: str) -> float:
    """Turn a generic mood score into a PRICE-direction score for a physical commodity.

    Generic sentiment models read "shortage worsens as disease hits farms" as very negative,
    but for the commodity's price that is bullish. When supply-shock language is present the
    direction comes from the supply logic and the model's intensity only scales magnitude.
    """
    t = text.lower()
    bullish = any(term in t for term in SUPPLY_BULLISH)
    bearish = any(term in t for term in SUPPLY_BEARISH)
    if bullish and not bearish:
        return clip(0.3 + 0.7 * abs(base))
    if bearish and not bullish:
        return clip(-(0.3 + 0.7 * abs(base)))
    return clip(base)


def is_food_shock(text: str) -> bool:
    t = text.lower()
    return any(term in t for term in FOOD_SHOCK_TERMS)


def tag_instruments(text: str, keyword_index: list[tuple[str, str]]) -> set[str]:
    t = text.lower()
    hits: set[str] = set()
    for kw, inst_id in keyword_index:
        # word-boundary match for short keywords, substring for multi-word phrases
        if " " in kw or len(kw) > 6:
            if kw in t:
                hits.add(inst_id)
        elif re.search(rf"\b{re.escape(kw)}\b", t):
            hits.add(inst_id)
    return hits


class SentimentScorer:
    def __init__(self, universe: Universe, engine_name: str = "vader", min_mentions: int = 2) -> None:
        self.universe = universe
        self.engine = make_engine(engine_name)
        self.kw_index = universe.keyword_index()
        self.min_mentions = max(1, int(min_mentions))

    def score_items(self, items: list[TextItem], as_of: datetime | None = None) -> list[Signal]:
        as_of = as_of or utcnow()
        buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)  # inst -> [(score, weight)]
        food_shock: dict[str, int] = defaultdict(int)
        examples: dict[str, list[str]] = defaultdict(list)

        for it in items:
            insts = tag_instruments(it.text, self.kw_index)
            if not insts:
                continue
            base = self.engine.score(it.text)
            for inst_id in insts:
                inst = self.universe.get(inst_id)
                # supply-shock logic only makes sense for physical commodities
                s = commodity_price_score(base, it.text) if inst and inst.asset_class == "futures" else base
                buckets[inst_id].append((clip(s), it.weight))
                if is_food_shock(it.text):
                    food_shock[inst_id] += 1
                if len(examples[inst_id]) < 3:
                    examples[inst_id].append(it.text[:140])

        signals: list[Signal] = []
        for inst_id, pairs in buckets.items():
            n = len(pairs)
            if n < self.min_mentions:
                continue
            wsum = sum(w for _, w in pairs) or 1.0
            mean = sum(s * w for s, w in pairs) / wsum
            # confidence grows with mention count, saturating around 10 mentions
            conf = min(1.0, n / 10.0)
            signals.append(Signal(
                source="sentiment", instrument_id=inst_id, score=mean, confidence=conf, as_of=as_of,
                evidence={"mentions": n, "food_shock_mentions": food_shock[inst_id],
                          "examples": examples[inst_id]},
            ))
        return signals
