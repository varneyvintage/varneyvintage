"""Price trend: a small, honest filter so the bot does not fade a freight train on sentiment alone."""
from __future__ import annotations

import math

from ..data.market import MarketData
from ..universe import Universe
from .base import Signal, clip, utcnow


def trend_score(close) -> float:
    if len(close) < 60:
        return 0.0
    fast = float(close.rolling(20).mean().iloc[-1])
    slow = float(close.rolling(50).mean().iloc[-1])
    vol = float(close.pct_change().rolling(20).std().iloc[-1]) or 1e-9
    spread = (fast - slow) / slow
    # normalise by ~1 month of volatility
    return math.tanh(spread / (vol * math.sqrt(20)))


class TrendSource:
    def __init__(self, universe: Universe, market: MarketData) -> None:
        self.universe = universe
        self.market = market

    def signals(self) -> list[Signal]:
        out = []
        for inst in self.universe:
            df = self.market.history(inst.data_symbol, days=120)
            if df.empty:
                continue
            s = trend_score(df["Close"])
            out.append(Signal(source="trend", instrument_id=inst.id, score=clip(s), confidence=0.7, as_of=utcnow()))
        return out
