"""Paper broker: fills at the last price plus a slippage/commission haircut; persists to JSON."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from ..strategy.risk import FUTURES_MULTIPLIER
from ..universe import Instrument
from .base import Fill, Position

log = logging.getLogger(__name__)


class PaperBroker:
    name = "paper"

    def __init__(self, state_path: Path, starting_equity: float, slippage_bps: float = 5.0,
                 commission_per_unit: float = 0.0) -> None:
        self.state_path = Path(state_path)
        self.slippage = slippage_bps / 10_000
        self.commission = commission_per_unit
        self.cash = starting_equity
        self._positions: dict[str, Position] = {}
        self._last: dict[str, float] = {}
        self._classes: dict[str, str] = {}
        self.fills: list[Fill] = []
        self._load()

    # ---- persistence --------------------------------------------------------------
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            d = json.loads(self.state_path.read_text())
        except json.JSONDecodeError:
            return
        self.cash = float(d.get("cash", self.cash))
        self._last = {k: float(v) for k, v in d.get("last", {}).items()}
        self._classes = dict(d.get("classes", {}))
        for p in d.get("positions", []):
            self._positions[p["instrument_id"]] = Position(
                instrument_id=p["instrument_id"], units=float(p["units"]), avg_price=float(p["avg_price"]),
                stop_price=p.get("stop_price"),
                opened_at=datetime.fromisoformat(p["opened_at"]) if p.get("opened_at") else None)

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        d = {
            "cash": self.cash, "last": self._last, "classes": self._classes,
            "positions": [{"instrument_id": p.instrument_id, "units": p.units, "avg_price": p.avg_price,
                           "stop_price": p.stop_price,
                           "opened_at": p.opened_at.isoformat() if p.opened_at else None}
                          for p in self._positions.values()],
            "updated": datetime.now(UTC).isoformat(),
        }
        self.state_path.write_text(json.dumps(d, indent=2))

    # ---- helpers --------------------------------------------------------------------
    def _mult(self, inst_id: str) -> float:
        cls = self._classes.get(inst_id, "equity")
        if cls == "futures":
            return FUTURES_MULTIPLIER.get(inst_id, 1)
        if cls == "fx":
            return 1000.0
        return 1.0

    def _pnl_per_unit(self, inst_id: str, entry: float, px: float) -> float:
        if self._classes.get(inst_id) == "fx":
            return (px - entry) * 1000.0  # USD P&L approximated for a USD-quoted pair
        return (px - entry) * self._mult(inst_id)

    # ---- Broker API ------------------------------------------------------------------
    def mark(self, prices: dict[str, float]) -> None:
        self._last.update(prices)

    def equity(self) -> float:
        unreal = sum(p.units * self._pnl_per_unit(p.instrument_id, p.avg_price, self._last.get(p.instrument_id, p.avg_price))
                     for p in self._positions.values())
        return self.cash + unreal

    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def trade_to(self, inst: Instrument, target_units: float, price_hint: float,
                 stop_price: float | None, reason: str) -> Fill | None:
        self._classes[inst.id] = inst.asset_class
        cur = self._positions.get(inst.id)
        cur_units = cur.units if cur else 0.0
        delta = target_units - cur_units
        if abs(delta) < 1e-9:
            if cur and stop_price is not None:
                cur.stop_price = stop_price
            return None
        px = price_hint * (1 + self.slippage) if delta > 0 else price_hint * (1 - self.slippage)
        now = datetime.now(UTC)
        commission = abs(delta) * self.commission
        # realise P&L on the part that closes/reduces the existing position
        if cur and cur_units * delta < 0:
            closing = min(abs(delta), abs(cur_units)) * (1 if cur_units > 0 else -1)
            self.cash += closing * self._pnl_per_unit(inst.id, cur.avg_price, px)
        self.cash -= commission
        new_units = cur_units + delta
        if abs(new_units) < 1e-9:
            self._positions.pop(inst.id, None)
        elif cur is None or cur_units * new_units <= 0:
            # new position or flipped side -> fresh average price
            self._positions[inst.id] = Position(inst.id, new_units, px, stop_price, now)
        elif abs(new_units) > abs(cur_units):
            # adding: volume-weighted average
            cur.avg_price = (cur.avg_price * abs(cur_units) + px * abs(delta)) / abs(new_units)
            cur.units, cur.stop_price = new_units, stop_price
        else:
            cur.units, cur.stop_price = new_units, stop_price
        fill = Fill(inst.id, delta, px, now, reason, commission)
        self.fills.append(fill)
        log.info("PAPER FILL %s %+.2f @ %.4f  (%s)", inst.id, delta, px, reason)
        self.save()
        return fill

    def check_stops(self, universe_lookup) -> list[Fill]:
        """Close any position whose stop has been crossed by the last marked price."""
        fills = []
        for inst_id, p in list(self._positions.items()):
            px = self._last.get(inst_id)
            if px is None or p.stop_price is None:
                continue
            hit = (p.units > 0 and px <= p.stop_price) or (p.units < 0 and px >= p.stop_price)
            if hit:
                inst = universe_lookup(inst_id)
                if inst:
                    f = self.trade_to(inst, 0.0, px, None, "stop hit")
                    if f:
                        fills.append(f)
        return fills

    def flatten_all(self, reason: str) -> list[Fill]:
        fills = []
        for inst_id, p in list(self._positions.items()):
            px = self._last.get(inst_id, p.avg_price)
            dummy = Instrument(inst_id, inst_id, self._classes.get(inst_id, "equity"), "", {}, None, ())
            f = self.trade_to(dummy, 0.0, px, None, reason)
            if f:
                fills.append(f)
        return fills
