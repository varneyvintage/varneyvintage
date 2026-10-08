"""Position sizing and kill switches for a high-risk profile.

Sizing: volatility-scaled. Risk budget per trade = risk_per_trade_pct * equity * |conviction|.
Stop distance = atr_stop_multiple * ATR. Units = risk budget / stop distance, then capped by
max_position_pct, max_gross_exposure_pct and max_open_positions.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from ..data.market import Bar
from .combiner import Target

log = logging.getLogger(__name__)

# Rough contract multipliers for notional maths on futures (price * multiplier = notional per contract).
# Approximate from memory - VERIFY against your broker's contract specs before live sizing.
FUTURES_MULTIPLIER = {
    "COCOA": 10, "COFFEE": 375, "COTTON": 500, "SUGAR": 1120, "MAIZE": 50, "WHEAT": 50,
    "GOLD": 100, "PLATINUM": 50, "PALLADIUM": 100, "COPPER": 25000, "BRENT": 1000,
}


@dataclass
class RiskState:
    peak_equity: float
    day: str
    day_start_equity: float
    halted: bool = False
    halt_reason: str = ""

    @classmethod
    def load(cls, path: Path, equity: float) -> RiskState:
        if path.exists():
            try:
                d = json.loads(path.read_text())
                return cls(**d)
            except Exception:
                pass
        return cls(peak_equity=equity, day=date.today().isoformat(), day_start_equity=equity)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))


@dataclass
class Order:
    instrument_id: str
    side: str            # buy | sell
    quantity: float      # positive; units (contracts / shares / FX lots)
    reason: str
    stop_price: float | None = None
    target_notional: float = 0.0


class RiskManager:
    def __init__(self, cfg: dict, state_path: Path, equity: float) -> None:
        self.cfg = cfg
        self.state_path = state_path
        self.state = RiskState.load(state_path, equity)

    # ---- kill switches ----------------------------------------------------------
    def update_equity(self, equity: float) -> None:
        s = self.state
        today = date.today().isoformat()
        if s.day != today:
            s.day, s.day_start_equity = today, equity
        s.peak_equity = max(s.peak_equity, equity)
        dd = (s.peak_equity - equity) / s.peak_equity * 100 if s.peak_equity > 0 else 0.0
        if dd >= float(self.cfg.get("max_drawdown_kill_pct", 30)):
            s.halted, s.halt_reason = True, f"max drawdown {dd:.1f}% hit"
            log.error("KILL SWITCH: %s", s.halt_reason)
        s.save(self.state_path)

    def can_open(self, equity: float) -> tuple[bool, str]:
        s = self.state
        if s.halted:
            return False, s.halt_reason
        day_pnl_pct = (equity - s.day_start_equity) / s.day_start_equity * 100 if s.day_start_equity else 0.0
        if day_pnl_pct <= -float(self.cfg.get("daily_loss_kill_pct", 8)):
            return False, f"daily loss {day_pnl_pct:.1f}% - no new positions today"
        return True, ""

    # ---- sizing ----------------------------------------------------------------
    def unit_value(self, inst_id: str, asset_class: str, price: float) -> float:
        if asset_class == "futures":
            return price * FUTURES_MULTIPLIER.get(inst_id, 1)
        if asset_class == "fx":
            return 1.0 * 1000  # size FX in 1,000-unit micro lots of base currency
        return price

    def desired_units(self, t: Target, bar: Bar, equity: float) -> tuple[float, float | None]:
        """Return (signed units, stop price). 0 if conviction is below threshold."""
        c = t.conviction
        if abs(c) < float(self.cfg.get("min_signal_conviction", 0.25)) or t.blocked:
            return 0.0, None
        if c < 0 and not self.cfg.get("allow_short", True):
            return 0.0, None
        if bar.atr != bar.atr or bar.atr <= 0:  # NaN guard
            return 0.0, None
        risk_budget = equity * float(self.cfg.get("risk_per_trade_pct", 2)) / 100 * abs(c)
        stop_dist = float(self.cfg.get("atr_stop_multiple", 2.5)) * bar.atr
        uv = self.unit_value(t.instrument.id, t.instrument.asset_class, bar.close)
        per_unit_risk = stop_dist * (uv / bar.close) if t.instrument.asset_class != "fx" else stop_dist * 1000
        units = risk_budget / per_unit_risk if per_unit_risk > 0 else 0.0
        # cap notional
        max_notional = equity * float(self.cfg.get("max_position_pct", 20)) / 100
        units = min(units, max_notional / uv) if uv > 0 else 0.0
        if t.instrument.asset_class in ("futures",):
            units = float(int(units))  # whole contracts
        elif t.instrument.asset_class in ("etf", "equity"):
            units = float(int(units))
        else:
            units = round(units, 2)
        stop = bar.close - stop_dist if c > 0 else bar.close + stop_dist
        return (units if c > 0 else -units), stop

    def portfolio_caps(self, desired: dict[str, tuple[float, float]], equity: float) -> dict[str, float]:
        """desired: inst -> (signed units, unit_value). Enforce gross/net exposure and position count."""
        max_gross = equity * float(self.cfg.get("max_gross_exposure_pct", 200)) / 100
        max_net = equity * float(self.cfg.get("max_net_exposure_pct", 150)) / 100
        max_n = int(self.cfg.get("max_open_positions", 6))
        # keep the biggest |notional| positions first
        ranked = sorted(desired.items(), key=lambda kv: -abs(kv[1][0] * kv[1][1]))[:max_n]
        gross = sum(abs(u * uv) for _, (u, uv) in ranked)
        net = sum(u * uv for _, (u, uv) in ranked)
        scale = 1.0
        if gross > max_gross > 0:
            scale = min(scale, max_gross / gross)
        if abs(net) > max_net > 0:
            scale = min(scale, max_net / abs(net))
        out: dict[str, float] = {}
        for inst_id, (u, _uv) in ranked:
            out[inst_id] = u * scale
        return out
