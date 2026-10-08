"""Instrument universe with Africa-linkage metadata."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class AfricaLink:
    countries: tuple[str, ...]
    note: str
    producer_benefits_from_rally: bool


@dataclass(frozen=True)
class Instrument:
    id: str
    name: str
    asset_class: str  # futures | fx | etf | equity
    data_symbol: str
    broker_symbol: dict
    africa_link: AfricaLink | None
    keywords: tuple[str, ...]
    verified: bool = False

    @property
    def is_african_equity(self) -> bool:
        return self.asset_class in ("etf", "equity") and self.africa_link is not None

    @property
    def is_african_fx(self) -> bool:
        return self.asset_class == "fx" and self.africa_link is not None


@dataclass
class Universe:
    instruments: dict[str, Instrument]
    macro_series: dict[str, dict]

    def __iter__(self) -> Iterable[Instrument]:
        return iter(self.instruments.values())

    def __len__(self) -> int:
        return len(self.instruments)

    def get(self, inst_id: str) -> Instrument | None:
        return self.instruments.get(inst_id.upper())

    def match_symbol(self, symbol: str) -> Instrument | None:
        """Map an external symbol (leader's ticker, broker symbol, data symbol) onto an instrument."""
        s = symbol.strip().upper()
        aliases = {
            "CC": "COCOA", "CCZ": "COCOA", "KC": "COFFEE", "CT": "COTTON", "SB": "SUGAR",
            "ZC": "MAIZE", "CORN": "MAIZE", "ZW": "WHEAT", "GC": "GOLD", "XAUUSD": "GOLD", "XAU": "GOLD",
            "PL": "PLATINUM", "XPTUSD": "PLATINUM", "PA": "PALLADIUM", "XPDUSD": "PALLADIUM",
            "HG": "COPPER", "BZ": "BRENT", "BRN": "BRENT", "CO": "BRENT", "OIL": "BRENT",
            "USD/ZAR": "USDZAR", "USD.ZAR": "USDZAR", "ZAR": "USDZAR",
        }
        if s in self.instruments:
            return self.instruments[s]
        if s in aliases:
            return self.instruments.get(aliases[s])
        for inst in self.instruments.values():
            if s == inst.data_symbol.upper():
                return inst
            for v in inst.broker_symbol.values():
                if v and s == str(v).upper():
                    return inst
        return None

    def keyword_index(self) -> list[tuple[str, str]]:
        """(lowercase keyword, instrument id) pairs, longest keywords first."""
        pairs = [(k.lower(), inst.id) for inst in self for k in inst.keywords]
        pairs.sort(key=lambda p: -len(p[0]))
        return pairs


def load_universe(path: str | Path) -> Universe:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    instruments: dict[str, Instrument] = {}
    for item in raw.get("instruments", []):
        link = item.get("africa_link")
        africa = (
            AfricaLink(
                countries=tuple(link.get("countries", [])),
                note=str(link.get("note", "")),
                producer_benefits_from_rally=bool(link.get("producer_benefits_from_rally", False)),
            )
            if link
            else None
        )
        inst = Instrument(
            id=str(item["id"]).upper(),
            name=item.get("name", item["id"]),
            asset_class=str(item.get("asset_class", "futures")).lower(),
            data_symbol=str(item["data_symbol"]),
            broker_symbol=dict(item.get("broker_symbol") or {}),
            africa_link=africa,
            keywords=tuple(str(k).lower() for k in item.get("keywords", [])),
            verified=bool(item.get("verified", False)),
        )
        instruments[inst.id] = inst
    return Universe(instruments=instruments, macro_series=dict(raw.get("macro_series", {})))
