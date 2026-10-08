"""Macro regime: USD strength, US rates, risk appetite, from free daily series.

Each factor is a z-scored momentum reading in [-1, 1]; each instrument class has betas to the
factors (config macro.class_betas). Score = sum(beta * factor). "Strong macros" here means the
regime reading moves the needle: in a strong-USD / risk-off regime this will lean against long
EM assets and commodities regardless of what the leaders are doing.
"""
from __future__ import annotations

import logging
import math

import pandas as pd

from ..data.market import MarketData
from ..universe import Universe
from .base import Signal, clip, utcnow

log = logging.getLogger(__name__)


def momentum_z(close: pd.Series, lookback: int = 60, fast: int = 20) -> float:
    """z-score of fast momentum against its own history; tanh-squashed to [-1, 1]."""
    if len(close) < lookback + fast:
        return 0.0
    ret = close.pct_change(fast).dropna()
    hist = ret.iloc[-lookback:]
    sd = float(hist.std()) or 1e-9
    z = (float(ret.iloc[-1]) - float(hist.mean())) / sd
    return math.tanh(z / 2.0)


class MacroRegime:
    def __init__(self, universe: Universe, market: MarketData, cfg: dict) -> None:
        self.universe = universe
        self.market = market
        self.cfg = cfg
        self.lookback = int(cfg.get("lookback_days", 60))
        self.class_betas: dict[str, dict[str, float]] = cfg.get("class_betas", {})

    def factors(self) -> dict[str, float]:
        f: dict[str, float] = {}
        series = self.universe.macro_series
        def mom(key: str) -> float:
            meta = series.get(key)
            if not meta:
                return 0.0
            df = self.market.history(meta["data_symbol"], days=self.lookback + 60)
            return momentum_z(df["Close"], self.lookback) if not df.empty else 0.0

        f["usd_strength"] = mom("DXY")
        f["us_rates"] = mom("US10Y")
        # risk appetite: EM equities up & VIX down
        f["risk_appetite"] = clip(0.5 * mom("EEM") - 0.5 * mom("VIX"))
        f["energy"] = mom("BRENT")
        f["africa_fx"] = -mom("USDZAR")   # rand strengthening = positive for Africa risk
        return f

    def signals(self) -> list[Signal]:
        f = self.factors()
        log.info("macro factors: %s", {k: round(v, 2) for k, v in f.items()})
        out: list[Signal] = []
        for inst in self.universe:
            betas = self.class_betas.get(inst.asset_class, {})
            score = sum(b * f.get(name, 0.0) for name, b in betas.items())
            # Africa-FX tailwind for African equities/ETFs
            if inst.asset_class in ("etf", "equity"):
                score += 0.3 * f["africa_fx"]
            out.append(Signal(source="macro", instrument_id=inst.id, score=clip(score), confidence=0.8,
                              as_of=utcnow(), evidence={"factors": f, "betas": betas}))
        return out
