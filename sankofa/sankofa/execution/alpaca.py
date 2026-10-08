"""Alpaca adapter: US-listed ETFs/ADRs only (EZA, GFI, HMY, SBSW, JMIA...). No futures.

Alpaca offers a free paper-trading endpoint, which makes it the easiest 'real API' to test
the pipeline against. Install with: pip install 'sankofa[alpaca]'.
This adapter is written against the alpaca-py SDK as I understand it and has NOT been run
against a live account from this environment - exercise it on paper first.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from ..universe import Instrument
from .base import Fill, Position

log = logging.getLogger(__name__)


class AlpacaBroker:
    name = "alpaca"

    def __init__(self, api_key: str | None = None, secret: str | None = None, paper: bool = True) -> None:
        from alpaca.trading.client import TradingClient

        self.client = TradingClient(api_key or os.environ["ALPACA_API_KEY"],
                                    secret or os.environ["ALPACA_SECRET_KEY"], paper=paper)
        self.paper = paper
        self._last: dict[str, float] = {}
        self._sym_to_inst: dict[str, str] = {}

    def mark(self, prices: dict[str, float]) -> None:
        self._last.update(prices)

    def equity(self) -> float:
        return float(self.client.get_account().equity)

    def positions(self) -> dict[str, Position]:
        out: dict[str, Position] = {}
        for p in self.client.get_all_positions():
            inst_id = self._sym_to_inst.get(p.symbol, p.symbol)
            qty = float(p.qty) * (1 if str(p.side).lower().endswith("long") else -1)
            out[inst_id] = Position(inst_id, qty, float(p.avg_entry_price))
        return out

    def trade_to(self, inst: Instrument, target_units: float, price_hint: float,
                 stop_price: float | None, reason: str) -> Fill | None:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        symbol = inst.broker_symbol.get("alpaca")
        if not symbol:
            log.info("alpaca: %s has no alpaca symbol (futures/fx not supported) - skipped", inst.id)
            return None
        self._sym_to_inst[symbol] = inst.id
        cur = self.positions().get(inst.id)
        delta = target_units - (cur.units if cur else 0.0)
        qty = int(abs(delta))
        if qty == 0:
            return None
        req = MarketOrderRequest(symbol=symbol, qty=qty,
                                 side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
                                 time_in_force=TimeInForce.DAY)
        order = self.client.submit_order(req)
        log.info("ALPACA %s order %s %s x%d (%s) id=%s", "PAPER" if self.paper else "LIVE",
                 req.side, symbol, qty, reason, order.id)
        # Stops: Alpaca supports bracket/stop orders, but a market order with a separate stop is
        # simplest and robust to partial fills. The engine re-evaluates stops every cycle anyway.
        return Fill(inst.id, delta if delta > 0 else -qty, price_hint, datetime.now(UTC), reason)

    def flatten_all(self, reason: str) -> list[Fill]:
        self.client.close_all_positions(cancel_orders=True)
        log.warning("ALPACA: closed all positions (%s)", reason)
        return []
