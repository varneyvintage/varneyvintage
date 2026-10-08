"""Interactive Brokers adapter for FUTURES and FX via the ib_async library (fork of ib_insync).

This is the only broker in the project that can actually trade ICE/CME/NYMEX commodity futures.
It needs TWS or IB Gateway running locally with API access enabled. Contract months are
resolved to the front month at runtime. This adapter is a careful first draft: it has NOT been
exercised against an IB account from this environment. Run it against an IB PAPER account
(port 7497 by default) for weeks before you consider live (port 7496).
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from ..universe import Instrument
from .base import Fill, Position

log = logging.getLogger(__name__)

# Exchange routing for the symbols in config/universe.yaml. Verify in TWS contract search.
IB_EXCHANGE = {
    "CC": "NYBOT", "KC": "NYBOT", "CT": "NYBOT", "SB": "NYBOT",
    "ZC": "CBOT", "ZW": "CBOT",
    "GC": "COMEX", "HG": "COMEX", "PL": "NYMEX", "PA": "NYMEX",
    "BZ": "NYMEX",  # Brent Last Day Financial; ICE Brent is symbol "COIL" on IPE
}


class IBKRBroker:
    name = "ibkr"

    def __init__(self, host: str | None = None, port: int | None = None, client_id: int | None = None) -> None:
        from ib_async import IB

        self.ib = IB()
        self.ib.connect(host or os.environ.get("IBKR_HOST", "127.0.0.1"),
                        int(port or os.environ.get("IBKR_PORT", "7497")),
                        clientId=int(client_id or os.environ.get("IBKR_CLIENT_ID", "7")))
        self._contracts: dict[str, object] = {}
        self._last: dict[str, float] = {}

    def _contract(self, inst: Instrument):
        from ib_async import ContFuture, Forex, Stock

        if inst.id in self._contracts:
            return self._contracts[inst.id]
        sym = inst.broker_symbol.get("ibkr")
        if inst.asset_class == "futures":
            c = ContFuture(sym, IB_EXCHANGE.get(sym, "SMART"))
        elif inst.asset_class == "fx":
            c = Forex(sym.replace(".", ""))
        else:
            c = Stock(sym, "SMART", "USD")
        q = self.ib.qualifyContracts(c)
        if not q:
            raise RuntimeError(f"IB could not qualify contract for {inst.id} ({sym})")
        self._contracts[inst.id] = q[0]
        return q[0]

    def mark(self, prices: dict[str, float]) -> None:
        self._last.update(prices)

    def equity(self) -> float:
        for row in self.ib.accountSummary():
            if row.tag == "NetLiquidation":
                return float(row.value)
        raise RuntimeError("IB NetLiquidation not available")

    def positions(self) -> dict[str, Position]:
        out: dict[str, Position] = {}
        by_con = {c.conId: inst_id for inst_id, c in self._contracts.items()}
        for p in self.ib.positions():
            inst_id = by_con.get(p.contract.conId, p.contract.localSymbol)
            out[inst_id] = Position(inst_id, float(p.position), float(p.avgCost))
        return out

    def trade_to(self, inst: Instrument, target_units: float, price_hint: float,
                 stop_price: float | None, reason: str) -> Fill | None:
        from ib_async import MarketOrder, StopOrder

        c = self._contract(inst)
        cur = self.positions().get(inst.id)
        delta = target_units - (cur.units if cur else 0.0)
        qty = abs(delta)
        if inst.asset_class != "fx":
            qty = int(qty)
        if qty == 0:
            return None
        action = "BUY" if delta > 0 else "SELL"
        if inst.asset_class == "fx":
            qty = qty * 1000  # units were 1,000-unit micro lots
        parent = MarketOrder(action, qty)
        parent.transmit = stop_price is None
        trade = self.ib.placeOrder(c, parent)
        if stop_price is not None and abs(target_units) > 0:
            stop = StopOrder("SELL" if target_units > 0 else "BUY", qty, round(stop_price, 4),
                             parentId=parent.orderId)
            self.ib.placeOrder(c, stop)
        self.ib.sleep(1)
        log.info("IBKR %s %s x%s (%s) status=%s", action, inst.id, qty, reason, trade.orderStatus.status)
        return Fill(inst.id, delta, price_hint, datetime.now(UTC), reason)

    def flatten_all(self, reason: str) -> list[Fill]:
        from ib_async import MarketOrder

        fills = []
        for p in self.ib.positions():
            if p.position == 0:
                continue
            action = "SELL" if p.position > 0 else "BUY"
            self.ib.placeOrder(p.contract, MarketOrder(action, abs(p.position)))
            fills.append(Fill(p.contract.localSymbol, -p.position, 0.0, datetime.now(UTC), reason))
        log.warning("IBKR: flattened %d positions (%s)", len(fills), reason)
        return fills
