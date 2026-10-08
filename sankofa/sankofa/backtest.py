"""Quick daily backtest of the trend + macro legs only.

Copy-trade and sentiment signals have no history to replay, so this is NOT a backtest of the
full strategy. It answers a narrower question: do the systematic legs plus the ethos rules and
risk sizing produce something sane on this universe? Treat results as a sanity check.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from .config import Settings
from .data.market import StaticMarketData, YahooMarketData, compute_atr
from .signals.base import Signal, SignalSet
from .signals.macro import MacroRegime
from .signals.trend import trend_score
from .strategy.combiner import Combiner
from .strategy.risk import RiskManager
from .universe import load_universe

log = logging.getLogger(__name__)


def run_backtest(settings: Settings, days: int = 365, rebalance_every: int = 5) -> pd.DataFrame:
    uni = load_universe(settings.universe_file)
    yd = YahooMarketData()
    frames = {inst.id: yd.history(inst.data_symbol, days=days + 150) for inst in uni}
    macro_frames = {k: yd.history(v["data_symbol"], days=days + 150) for k, v in uni.macro_series.items()}
    frames = {k: v for k, v in frames.items() if not v.empty}
    if not frames:
        raise RuntimeError("no price data (network/yfinance issue)")
    idx = sorted(set.intersection(*[set(f.index) for f in frames.values()]))[-days:]
    equity = settings.starting_equity
    cash, units = equity, {k: 0.0 for k in frames}
    combiner = Combiner(uni, settings.signals, settings.ethos)
    rm = RiskManager(settings.risk, settings.state_dir / "_bt_risk.json", equity)
    rm.state.peak_equity, rm.state.day_start_equity, rm.state.halted = equity, equity, False
    rows = []
    for i, day in enumerate(idx):
        px = {k: float(f.loc[day, "Close"]) for k, f in frames.items()}
        equity = cash + sum(units[k] * rm.unit_value(k, uni.get(k).asset_class, px[k]) - units[k] * rm.unit_value(k, uni.get(k).asset_class, 0) * 0
                            for k in frames)
        if i % rebalance_every == 0:
            # build signals as of `day`
            sigs = SignalSet()
            hist = {k: f.loc[:day] for k, f in frames.items()}
            mstatic = StaticMarketData({v["data_symbol"]: macro_frames[k].loc[:day] for k, v in uni.macro_series.items() if not macro_frames[k].empty})
            for s in MacroRegime(uni, mstatic, settings.macro).signals():
                s.as_of = datetime.now(UTC)
                sigs.add(s)
            for k, h in hist.items():
                sigs.add(Signal("trend", k, trend_score(h["Close"]), 0.7))
            targets = combiner.combine(sigs)
            desired = {}
            for t in targets:
                h = hist.get(t.instrument.id)
                if h is None or len(h) < 20:
                    continue
                from .data.market import Bar
                bar = Bar(close=px[t.instrument.id], high=float(h["High"].iloc[-1]), low=float(h["Low"].iloc[-1]), atr=compute_atr(h))
                u, _ = rm.desired_units(t, bar, equity)
                if u:
                    desired[t.instrument.id] = (u, rm.unit_value(t.instrument.id, t.instrument.asset_class, bar.close))
            capped = rm.portfolio_caps(desired, equity)
            # rebalance: mark-to-market in cash terms
            for k in frames:
                target = capped.get(k, 0.0)
                if uni.get(k).asset_class in ("futures", "etf", "equity"):
                    target = float(int(target))
                delta = target - units[k]
                if delta:
                    cash -= delta * rm.unit_value(k, uni.get(k).asset_class, px[k]) * (1 + 0.0005 * np.sign(delta))
                    units[k] = target
        equity = cash + sum(units[k] * rm.unit_value(k, uni.get(k).asset_class, px[k]) for k in frames)
        rows.append({"date": day, "equity": equity, "gross": sum(abs(units[k] * rm.unit_value(k, uni.get(k).asset_class, px[k])) for k in frames)})
    df = pd.DataFrame(rows).set_index("date")
    ret = df["equity"].pct_change().dropna()
    summary = {
        "start": float(df["equity"].iloc[0]), "end": float(df["equity"].iloc[-1]),
        "total_return_pct": (df["equity"].iloc[-1] / df["equity"].iloc[0] - 1) * 100,
        "max_drawdown_pct": ((df["equity"] / df["equity"].cummax()) - 1).min() * 100,
        "sharpe_naive": (ret.mean() / ret.std() * np.sqrt(252)) if ret.std() else float("nan"),
    }
    log.info("backtest summary: %s", {k: round(v, 2) for k, v in summary.items()})
    df.attrs["summary"] = summary
    return df
