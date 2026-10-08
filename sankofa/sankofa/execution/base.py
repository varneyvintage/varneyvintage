from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..universe import Instrument


@dataclass
class Position:
    instrument_id: str
    units: float              # signed
    avg_price: float
    stop_price: float | None = None
    opened_at: datetime | None = None


@dataclass
class Fill:
    instrument_id: str
    units: float              # signed change
    price: float
    at: datetime
    reason: str
    commission: float = 0.0


class Broker(Protocol):
    name: str

    def equity(self) -> float: ...
    def positions(self) -> dict[str, Position]: ...
    def mark(self, prices: dict[str, float]) -> None: ...
    def trade_to(self, inst: Instrument, target_units: float, price_hint: float,
                 stop_price: float | None, reason: str) -> Fill | None: ...
    def flatten_all(self, reason: str) -> list[Fill]: ...
