"""Shared signal types. Every source emits per-instrument scores in [-1, 1]."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol


def utcnow() -> datetime:
    return datetime.now(UTC)


def clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


@dataclass
class Signal:
    source: str                # copytrade | sentiment | macro | trend
    instrument_id: str
    score: float               # -1 (max short) .. +1 (max long)
    confidence: float = 1.0    # 0..1, how much the source trusts this reading
    as_of: datetime = field(default_factory=utcnow)
    evidence: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.score = clip(self.score)
        self.confidence = clip(self.confidence, 0.0, 1.0)

    def age_minutes(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        return (now - self.as_of).total_seconds() / 60.0


@dataclass
class SignalSet:
    signals: list[Signal] = field(default_factory=list)

    def add(self, *sigs: Signal) -> None:
        self.signals.extend(sigs)

    def by_source(self, source: str) -> list[Signal]:
        return [s for s in self.signals if s.source == source]

    def for_instrument(self, inst_id: str) -> list[Signal]:
        return [s for s in self.signals if s.instrument_id == inst_id]

    def instruments(self) -> set[str]:
        return {s.instrument_id for s in self.signals}


class SignalSource(Protocol):
    name: str

    def collect(self) -> list[Signal]: ...
