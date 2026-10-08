"""Market data. Free source is Yahoo Finance via yfinance (delayed, unofficial, can break).

For live trading you should feed prices from your broker instead; the MarketData interface
is deliberately tiny so that is a small change.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class Bar:
    close: float
    high: float
    low: float
    atr: float  # ATR(14) in price units


class MarketData(Protocol):
    def history(self, symbol: str, days: int = 120) -> pd.DataFrame: ...
    def last(self, symbol: str) -> Bar | None: ...


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return float("nan")
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


class YahooMarketData:
    def __init__(self) -> None:
        self._cache: dict[str, pd.DataFrame] = {}

    def history(self, symbol: str, days: int = 120) -> pd.DataFrame:
        if symbol in self._cache:
            return self._cache[symbol]
        try:
            import yfinance as yf

            df = yf.download(symbol, period=f"{max(days, 30)}d", interval="1d",
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna()
        except Exception as exc:  # network, rate-limit, delisted
            log.warning("yfinance failed for %s: %s", symbol, exc)
            df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        self._cache[symbol] = df
        return df

    def last(self, symbol: str) -> Bar | None:
        df = self.history(symbol)
        if df.empty:
            return None
        row = df.iloc[-1]
        return Bar(close=float(row["Close"]), high=float(row["High"]), low=float(row["Low"]),
                   atr=compute_atr(df))

    def clear(self) -> None:
        self._cache.clear()


class StaticMarketData:
    """For tests and offline runs: dict of symbol -> DataFrame."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames

    def history(self, symbol: str, days: int = 120) -> pd.DataFrame:
        return self.frames.get(symbol, pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]))

    def last(self, symbol: str) -> Bar | None:
        df = self.history(symbol)
        if df.empty:
            return None
        row = df.iloc[-1]
        return Bar(close=float(row["Close"]), high=float(row["High"]), low=float(row["Low"]),
                   atr=compute_atr(df))
